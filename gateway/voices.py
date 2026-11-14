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
from pathlib import Path
from typing import Any

import httpx

from .config import (
    CUSTOM_PROVIDERS_PATH,  # noqa: F401  (re-exported for callers)
    DATA_DIR,
    VOICES_PATH,
    WRITABLE_VOICES_PATH,
    write_secret,
)
from .constants import zero_pricing
from .pricing import definition_chargeable
from .providers import registry
from .providers.definitions import (
    KIND_ELEVENLABS,
    KIND_FAL,
    KIND_FISH,
    KIND_GOOGLE,
    KIND_MINIMAX,
    KIND_OPENAI_COMPATIBLE,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)

#: The container each kind's TTS lane actually returns, read from the adapter's
#: own media type: ElevenLabs, fal's ElevenLabs routes and MiniMax answer MPEG
#: audio; Fish asks for WAV; a Studio Hub location and the OpenAI-compatible shim
#: (which Google's TTS delegates to) answer WAV.
#:
#: This used to be guessed from the provider id —
#: `"mp3" if provider_id.startswith("elevenlabs") else "wav"` — so a fal
#: ElevenLabs voice published `wav` while `adapters/fal.py` downloaded
#: `audio/mpeg`, and one fal lane's voice list contained rows claiming both. A
#: kind with no TTS lane gets no answer rather than a default.
#:
#: ponytail: a per-kind table, not a per-model measurement. Ceiling — a lane that
#: changes container for one model is described wrongly. Upgrade path — the
#: adapter's `BackendResult.media_type` for the model, which is the value the job
#: actually produced.
_TTS_CONTAINER_BY_KIND: dict[str, str] = {
    KIND_ELEVENLABS: "mp3",
    KIND_FAL: "mp3",
    KIND_MINIMAX: "mp3",
    KIND_FISH: "wav",
    KIND_STUDIO_HUB: "wav",
    KIND_OPENAI_COMPATIBLE: "wav",
    KIND_GOOGLE: "wav",
}


def tts_output_container(provider_id: str) -> str | None:
    """The audio container a provider's TTS lane returns, or `None` if unstated.

    One read, used by both the voice row and the TTS quote, so the two cannot
    describe the same model differently — which is exactly what they did.
    """
    definition = registry.definition(provider_id)
    if definition is None:
        return None
    return _TTS_CONTAINER_BY_KIND.get(definition.kind)

STATE_PATH = DATA_DIR / "voices_state.json"
DISCOVERY_TTL_S = 900.0
TIMEOUT = httpx.Timeout(30.0, connect=8.0)
PREVIEW_TIMEOUT = httpx.Timeout(30.0, connect=8.0)

ID_SEPARATOR = "::"

#: A voice tag is a short, plain label. These are the limits the editor prints and
#: the server enforces — one copy, published on `/admin/api/voices` as
#: `tag_limits`, so the hint beside the input cannot drift from the refusal.
TAG_MAX_LENGTH = 32
MAX_TAGS = 12


def clean_tags(values: Any) -> tuple[str, ...]:
    """Free-form voice tags: trimmed, de-duplicated case-insensitively, bounded.

    The first spelling of a duplicate wins (`Khmer` before `khmer`). Blank entries
    are dropped rather than stored as empty tags. A value that is not a list, a
    non-string entry, a control character, a tag over `TAG_MAX_LENGTH` or more than
    `MAX_TAGS` tags is **refused** — an edit that silently discards half a list
    would leave the owner looking at a tag he did not type.
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError("Tags must be a list of short strings.")
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("Every tag must be text.")
        tag = " ".join(raw.split())
        if not tag:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in tag):
            raise ValueError("A tag cannot contain control characters.")
        if len(tag) > TAG_MAX_LENGTH:
            raise ValueError(f"Each tag is at most {TAG_MAX_LENGTH} characters.")
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"A voice has at most {MAX_TAGS} tags.")
    return tuple(out)


def read_tags(values: Any) -> tuple[str, ...]:
    """Tags as read from stored data: **lenient**, because a voice must still load.

    Old voices have no tags (`()`), and a hand-edited catalogue cannot make a voice
    disappear from the library by carrying one bad entry — the unusable entries are
    dropped and the rest stay.
    """
    if not isinstance(values, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        tag = " ".join(raw.split())
        if (not tag or len(tag) > TAG_MAX_LENGTH
                or any(ord(char) < 32 or ord(char) == 127 for char in tag)):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) == MAX_TAGS:
            break
    return tuple(out)


def tag_suggestions(voices: list["Voice"]) -> list[str]:
    """Every tag in use, one spelling per case-insensitive tag, sorted for a picker."""
    seen: dict[str, str] = {}
    for voice in voices:
        for tag in voice.tags:
            seen.setdefault(tag.casefold(), tag)
    return sorted(seen.values(), key=str.casefold)

#: Where a voice came from. Drives both display and what can be done with it.
SOURCE_LOCAL = "local"        # this Mac's own reference clips
SOURCE_ACCOUNT = "account"    # the owner's own entry at the provider
SOURCE_LIBRARY = "library"    # the provider's public library, not yet owned
#: A name that appears only in a provider's **documentation** — fal's twenty-one
#: `examples` for its ElevenLabs TTS routes, which fal never returns as account
#: entries. It is neither the owner's stock nor something the provider will list:
#: it is a documented example this gateway curates so the lane has a voice to
#: name. Published as its own source so the row cannot be read as the owner's own
#: account voice (`voice_tier: "custom"`, a `custom_voice_id`) purely because the
#: call succeeded. See `ownership` in `story_row`.
SOURCE_EXAMPLE = "example"

#: What Story's picker is told a gateway-held (`SOURCE_LOCAL`) voice comes from.
GATEWAY_VOICE_LIBRARY = "Gateway voice library"


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
    #: Free-form labels for finding a voice in a long library. Never used for
    #: dispatch or ranking; an old voice with none reads as `()`.
    tags: tuple[str, ...] = ()
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
            "tags": list(self.tags),
            "has_preview": has_preview(self),
            "guest_owner": (self.raw or {}).get("guest_owner"),
            "guest_status": (self.raw or {}).get("guest_status"),
            "guest_error": (self.raw or {}).get("guest_error"),
            "owned": self.owned,
            # **Not `tts_model_id`.** That name is a *serving* claim — Story
            # Studio drops any voice whose `tts_model_id` is not the model being
            # asked about — and a local voice serves every model that declares
            # cloning. What this stamp actually records is the tier the clip was
            # imported and annotated against, which decides nothing at render
            # time, so it is published under a name that says so.
            "minted_against_model": (self.raw or {}).get("tts_model_id"),
            # The clip's own length: measured at import, or read from the WAV header.
            # Reported rather than inferred: a clip that cannot be measured says
            # `None` instead of a plausible guess.
            "duration_seconds": (self.raw or {}).get("duration_seconds"),
            # When a local voice's clip was imported (epoch seconds); `None` elsewhere.
            "imported_at": (self.raw or {}).get("imported_at"),
            # **These three used to live only in `raw`, which is never
            # serialised.** The menu bar app read `row["raw"]["owner_id"]` and
            # `row["raw"]["permission_acknowledged"]`, so it sent no owner for an
            # ElevenLabs library voice (every import 422'd, and the error was
            # swallowed) and every local voice rendered "Private" for ever, even
            # after the grant succeeded. Published top-level now, because a field
            # a reader needs is part of the contract, not an implementation detail.
            "owner_id": (self.raw or {}).get("owner_id"),
            # `None`, not `False`, for a voice whose permission does not apply:
            # a provider voice is not ours to grant, and saying `False` would
            # render it as "Private" — a definite claim about a question nobody
            # asked. Three answers, as everywhere else in this file.
            "permission_acknowledged": (
                bool((self.raw or {}).get("permission_acknowledged"))
                if self.source == SOURCE_LOCAL
                else None
            ),
            "license": (self.raw or {}).get("license"),
            # **What the provider said about using it, or nothing.** A usage suggestion
            # ("narration", "social media") and emotion/tone tags are published only when
            # the provider stated them; a reader prints "not provided" for `None`, never an
            # invented one-line pitch (0.2.257 voice picker).
            "usage_suggestion": ((self.raw or {}).get("usage_suggestion")
                                 if isinstance((self.raw or {}).get("usage_suggestion"), str)
                                 else None),
            "emotion_tags": (list((self.raw or {}).get("emotion_tags") or [])
                             if isinstance((self.raw or {}).get("emotion_tags"), list) else None),
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
                    raw={
                        "category": item.get("category"),
                        # ElevenLabs states these in the voice's own labels; pass them on
                        # rather than composing a description here.
                        "usage_suggestion": (str(labels.get("use case") or "").strip() or None),
                        "emotion_tags": [
                            part.strip() for part in str(labels.get("descriptive") or "").split(",")
                            if part.strip()
                        ] or None,
                    },
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

    **They are `SOURCE_EXAMPLE`, not `SOURCE_ACCOUNT`, and not owned.** fal never
    returned them as account entries; they are documentation examples. Publishing
    them as the owner's own voices gave every one `voice_tier: "custom"` and a
    `custom_voice_id`, which claims an account relationship that does not exist —
    and would make a fleet push look authorised for a voice the owner never added.
    """
    return [
        Voice(
            id=make_id(definition.id, name),
            provider_id=definition.id,
            provider_voice_id=name,
            display_name=name,
            source=SOURCE_EXAMPLE,
            language="",
            description="ElevenLabs voice, served through fal (fal's own example)",
            owned=False,
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
    from .voice_import import clip_seconds, imported_at  # lazy: voice_import reaches back into this module

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
                tags=read_tags(entry.get("tags")),
                raw={
                    "engine_name": entry.get("engine_name"),
                    "tts_model_id": entry.get("tts_model_id"),
                    # How long the recording is, as measured when it was imported.
                    # Needed to say whether a model's window will cut it, and it
                    # is the *clip* length rather than the speech span the
                    # segments describe.
                    "duration_seconds": clip_seconds(entry),
                    # When the clip came in. With the length, what tells two voices
                    # with the same name apart in the Voices list.
                    "imported_at": imported_at(entry),
                    # Carried through so the Studio Hub adapter can stage the
                    # clone reference without re-reading the catalogue file.
                    "reference_path": entry.get("reference_path"),
                    "reference_transcript": entry.get("reference_transcript"),
                    "reference_segments": entry.get("reference_segments"),
                    # False: the owner chose audio only; the adapter sends no words.
                    "include_reference_transcript": entry.get("include_reference_transcript") is not False,
                    # Whether the owner has granted permission to clone and
                    # distribute this voice. False until he says so, per voice.
                    "permission_acknowledged": bool(entry.get("permission_acknowledged")),
                    "license": entry.get("license"),
                    "guest_owner": entry.get("guest_owner"),
                    "guest_status": entry.get("guest_status"),
                    "guest_error": entry.get("guest_error"),
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


def has_preview(voice: Voice) -> bool:
    """A stored preview: the provider's own sample, or a local voice's reference clip on disk."""
    if voice.preview_url:
        return True
    path = (voice.raw or {}).get("reference_path") if voice.source == SOURCE_LOCAL else None
    return bool(path) and Path(str(path)).exists()


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
        # `hidden`: provider voices the owner removed from this Gateway ({provider: {voice: info}}).
        # They stay in the provider's account; Restore brings them back.
        self._data: dict[str, Any] = {"enabled": {}, "discovered": {}, "custom": {}, "hidden": {},
                                      "tags": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(parsed, dict):
            for key in ("enabled", "discovered", "custom", "hidden", "tags"):
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
    def _tags_for(self, provider_id: str, provider_voice_id: str) -> tuple[str, ...]:
        """A provider voice's tags, from this registry's own state.

        Kept apart from the discovered listing: a refresh replaces discovery, and a
        tag the owner typed must not vanish because a provider answered late. The
        bucket is keyed by the provider's own voice id, never by the display name.
        """
        return read_tags((self._data["tags"].get(provider_id) or {}).get(provider_voice_id))

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
                    tags=self._tags_for(provider_id, str(item.get("provider_voice_id") or "")),
                    raw=item.get("raw") or {},
                )
            )
        return out

    def _merged(self, provider_id: str) -> list[Voice]:
        """One row per voice id. The owner's own entry wins.

        `_from_cached` and `_custom` are the same list seen twice: discovery stores
        what the provider's API returned, and "Add to my account" stores the voice
        the owner just imported. A voice that appeared in both was listed **twice** —
        and since `find()` takes the first match, which row you got (and with it the
        preview and the tier shown) depended on which list happened to hold it. Two
        rows for one voice is also two chances to disagree with itself.

        Keyed on the id, which both sides build with `make_id`, so the two lists
        really are talking about the same voice and not merely the same name.
        """
        merged: dict[str, Voice] = {}
        for voice in self._from_cached(provider_id):
            merged[voice.id] = voice
        for voice in self._custom(provider_id):
            merged[voice.id] = voice
        # A removed voice is gone from every list and from `find`, so it cannot be used either.
        hidden = self._data["hidden"].get(provider_id) or {}
        return [v for v in merged.values() if v.provider_voice_id not in hidden]

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
                        tags=self._tags_for(provider_id, str(item.get("provider_voice_id") or "")),
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
        return voice.source == SOURCE_LOCAL and not (voice.raw or {}).get("guest_owner")

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
            out.extend(self._merged(definition.id))
        if not include_disabled:
            out = [v for v in out if self.is_enabled(v)]
        return sorted(out, key=lambda v: (v.provider_id, v.display_name.lower()))

    def enabled_voices(self) -> list[Voice]:
        return [v for v in self.voices(include_disabled=False)
                if not (v.raw or {}).get("guest_owner") or (v.raw or {}).get("guest_status") == "ready"]

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

    def set_tags(self, voice_id: str, tags: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """Replace one **provider** voice's tags. Local voices live in their catalogue.

        The stored list is the cleaned one, so the caller can show exactly what was
        kept. Clearing removes the bucket entry rather than storing an empty list;
        an old voice with none reads as `[]` either way.
        """
        provider_id, provider_voice_id = split_id(voice_id)
        if not provider_voice_id:
            raise KeyError(voice_id)
        cleaned = clean_tags(list(tags))
        if cleaned:
            self._data["tags"].setdefault(provider_id, {})[provider_voice_id] = list(cleaned)
        else:
            bucket = self._data["tags"].get(provider_id) or {}
            bucket.pop(provider_voice_id, None)
            if not bucket:
                self._data["tags"].pop(provider_id, None)
        self._save()
        return {"id": voice_id, "tags": list(cleaned)}

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

    def hide(self, voice: Voice) -> None:
        """Remove a provider voice from this Gateway. Nothing changes at the provider."""
        bucket = self._data["hidden"].setdefault(voice.provider_id, {})
        bucket[voice.provider_voice_id] = {"display_name": voice.display_name, "hidden_at": time.time()}
        self._save()

    def hidden_voices(self) -> list[dict[str, Any]]:
        return sorted(
            ({"id": make_id(provider_id, voice_id), "provider_id": provider_id,
              "display_name": str(info.get("display_name") or voice_id), "removed_at": info.get("hidden_at")}
             for provider_id, bucket in self._data["hidden"].items()
             for voice_id, info in (bucket or {}).items() if isinstance(info, dict)),
            key=lambda row: (row["provider_id"], row["display_name"].lower()))

    def unhide(self, voice_id: str) -> bool:
        provider_id, provider_voice_id = split_id(voice_id)
        found = (self._data["hidden"].get(provider_id) or {}).pop(provider_voice_id, None) is not None
        if found:
            self._save()
        return found

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
    def preview(self, voice_id: str, *, stored_only: bool = False) -> tuple[bytes, str]:
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
                    # By the bytes, not the name: a QuickTime container named `.mp3`
                    # served as audio/mpeg is a clip the app's player cannot open.
                    from . import voice_audio

                    try:
                        playable, media = voice_audio.playable(candidate)
                    except RuntimeError as exc:
                        raise KeyError(f"the reference clip could not be made playable: {exc}") from exc
                    return playable.read_bytes(), media

        if voice.preview_url:
            response = httpx.get(voice.preview_url, timeout=PREVIEW_TIMEOUT, follow_redirects=True)
            if response.status_code < 400 and response.content:
                media = response.headers.get("content-type", "").split(";")[0].strip()
                return response.content, media or "audio/mpeg"

        if stored_only:
            raise KeyError("No stored sample is available.")
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

        Provider voices are not tied to one model, so something has to be named.
        **An enabled discovered row is preferred over the curated list**, because a
        discovered model is one the provider currently offers and the curated list
        is a static fallback — ElevenLabs publishes *no* `static_models`, so this
        returned `""` and every preview for a discovered voice was synthesised
        against an empty model id, which the provider answers 404 for. A preview
        that always fails is a control that does nothing.
        """
        for model in registry.models.models_for(definition.id, include_disabled=False):
            if model.get("modality") == "tts" and model.get("enabled"):
                return str(model["model_id"])
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
    said that while the row hand-listed only `credits_per_1000_characters`,
    `minimum_credits`, `price_version` and `chargeable` — so `cost_tier` and
    `standard_credits` were absent, which is a **second copy of a shape that is
    defined once** in `constants.zero_pricing`. It is merged from there now, the
    same way every catalogue row gets its prices, so the two cannot drift again.
    `chargeable` especially: the TTS quote route already publishes it explicitly
    because the parser tests `=== true`, and an absent field renders a claim the
    gateway never made.
    """
    definition = registry.definition(voice.provider_id)
    # **A gateway-held voice is the Gateway's, not a Studio Hub's and not one family's.** Since
    # 0.2.101 its reference clip travels with the job to any Mac and works with every
    # cloning-capable model, so "Studio Hub (this Mac)" / "Qwen3 TTS" (the model it was minted
    # against, kept in `raw` as a stamp) described a tie that no longer exists. Story's picker
    # shows `provider · engine_name`: the source is honest, the family is empty (no single tie;
    # the models it works with are the ones whose rows say they clone), `id` is unchanged.
    held = voice.source == SOURCE_LOCAL
    engine = "" if held else str((voice.raw or {}).get("engine_name") or "")
    if not engine and not held:
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
        # **`""` when nothing was stated**, never "neutral": the catalogue stores nothing for
        # an unstated voice, and the picker prints only what is there.
        "gender": voice.gender or "",
        # Free-form labels the owner gave this voice. Never a dispatch or ranking input:
        # published so Story and a Hub can show the same words the Gateway shows.
        "tags": list(voice.tags),
        "description": voice.description or "",
        "provider": GATEWAY_VOICE_LIBRARY if held else (
            definition.display_name if definition else voice.provider_id),
        "engine_name": engine,
        "engine_id": voice.provider_id,
        "model_family": None if held else voice.source,
        "model_revision_id": None,
        # **Ownership decides the tier, in one place.** This was
        # `"custom" if source != SOURCE_LIBRARY else "library"`, which made any
        # new source "custom" by default — including fal's documentation examples,
        # once they stopped claiming to be account entries. `owned` is the actual
        # question ("is this the owner's own entry?"), and both this row and the
        # TTS quote read it, so the same voice cannot be described two ways.
        "voice_tier": "custom" if voice.owned else "library",
        "voice_revision": 1,
        "custom_voice_id": voice.provider_voice_id if voice.owned else None,
        "public_voice_id": None if voice.owned else voice.provider_voice_id,
        # **One definition of the zero-price shape**, merged rather than retyped.
        # This row used to hand-list three of the five keys and was already
        # missing `cost_tier` and `standard_credits` — the drift `zero_pricing`
        # exists to stop, and a drift that is invisible because an absent price
        # field makes Story Studio's parser drop the row.
        **zero_pricing("tts"),
        # The shape above says `true`; a voice on the owner's Macs costs nothing, so say so.
        "chargeable": definition is None or definition_chargeable(definition, "tts"),
        # **The adapter's real container, never a guess from the provider id.**
        # This said `"mp3" if provider_id.startswith("elevenlabs") else "wav"`, so
        # a fal ElevenLabs voice published `wav` while `adapters/fal.py` downloaded
        # `audio/mpeg`. `None` when the kind has no TTS lane at all — an absent
        # answer rather than a default.
        "output_format": tts_output_container(voice.provider_id),
        # MUST be exactly 'published': Story Studio treats any other value as a
        # generation gate and silently hides the voice.
        "catalog_status": "published",
        "quote_enabled": True,
        "generation_enabled": True,
        # **Only when a stored preview exists**: Story enables its preview button from this flag
        # and downloads `preview_url`, so a flag that was true for every local voice and a URL that
        # was never published made the button always fail. Nothing here synthesises on demand.
        "preview_available": has_preview(voice),
        "preview_url": f"/v1/voices/{voice.id}/preview" if has_preview(voice) else None,
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
    found: list[dict[str, Any]] = []
    for candidate in candidates:
        for model in registry.models.models_for(candidate, include_disabled=True):
            if str(model.get("model_id")) == model_id:
                found.append(model)
    if not found:
        return None
    # **A location that can publish it answers first.** Every location's copy of a
    # model carries its own Hub's contract, and returning the first one meant the
    # voice picker and the reference report could describe a site the Hub says
    # cannot run this model at all (`catalogued: false`, or too big for its
    # machine). The merged row is offered because *some* site can serve it, so the
    # record that describes it should be one of those.
    for model in found:
        raw = model.get("raw") or {}
        if (raw.get("catalogued") is False or raw.get("memory_eligible") is False
                or raw.get("ready_on_a_mac") is False):
            continue
        return model
    # Every copy is withheld. Returning one is still better than `None`: `None`
    # means *unverified*, which offers the voice, and the row already exists.
    return found[0]


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


#: The kinds whose TTS models clone even when the provider states nothing per
#: model. Today that is a **provider** fact for fish and ElevenLabs (their whole
#: TTS line takes a reference) and for a Studio Hub location, whose catalogue is
#: read per model but may be older than the gateway.
#:
#: ponytail: a hand-listed set. Ceiling — a cloning-capable provider outside
#: these three is under-described and its rows publish `supports_voice_cloning:
#: false` until it states the fact per model. Upgrade path — delete this set the
#: day every provider answers `reference_support`/`reference_audio`, which is
#: already how the local lane works.
CLONING_KINDS = frozenset({KIND_STUDIO_HUB, KIND_FISH, KIND_ELEVENLABS})


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


def supports_cloning(provider_id: str, model_id: str) -> bool:
    """The two-valued answer the catalogue must publish, from the same read.

    The catalogue's `supports_voice_cloning` has to be a bool, because Story
    Studio renders it as a fact about the model. `reference_support` has three
    answers, because that is what the evidence supports. This is the one bridge
    between them: an unstated model falls back to its kind's default, and nothing
    else is allowed to decide.

    It exists because the rule was written **twice** — `catalog._supports_cloning`
    read the model's own `raw` and this module read the registry — so the
    published flag and the voice picker could disagree, and a row claiming
    `supports_voice_cloning: true` could sit beside an empty picker with no error
    anywhere. One read, two shapes, in one place.
    """
    support = reference_support(provider_id, model_id)
    if support is not None:
        return support
    definition = registry.definition(provider_id)
    return definition is not None and definition.kind in CLONING_KINDS


def voices_for_model(compound_model_id: str, *, guest_id: str = "") -> list[dict[str, Any]]:
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
    available = [v for v in voice_registry().voices() if
                 (not (v.raw or {}).get("guest_owner") and voice_registry().is_enabled(v)) or
                 (bool(guest_id) and ((v.raw or {}).get("guest_owner") or {}).get("guest_id") == guest_id) or
                 (not guest_id and bool((v.raw or {}).get("guest_owner")) and voice_registry().is_enabled(v)
                  and (v.raw or {}).get("guest_status") == "ready")]
    if not provider_id:
        return [story_row(v) for v in available]

    accepted = accepted_voice_providers(provider_id, raw_model)
    takes_reference = reference_support(provider_id, raw_model)

    rows: list[dict[str, Any]] = []
    for voice in available:
        if not ({voice.provider_id, publish_id(voice.provider_id)} & accepted):
            continue
        if voice.source == SOURCE_LOCAL and takes_reference is False:
            # The model states it does not clone (preset speakers, voice design,
            # a fixed voicepack). A reference offered here is a control that
            # fails on use, so it is withheld rather than advertised.
            continue
        rows.append(story_row(voice))
    return rows
