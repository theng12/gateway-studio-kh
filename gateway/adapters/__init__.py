"""Adapter resolution: provider `kind` -> backend implementation.

Implemented and wired:

    openai_compatible  OpenRouter, DeepSeek, OpenAI, Groq, NVIDIA NIM,
                       Token Harbor, Experiential Labs, xAI
    anthropic          Anthropic
    google             Google AI (via its OpenAI-compatible endpoint)
    fal                fal.ai
    kie                Kie.ai
    fish               Fish Audio (TTS, model selected by HTTP header)
    studio_hub         Studio Hub (local)

Not yet wired (they surface a clear error rather than failing silently):

    bfl, minimax, elevenlabs

Those providers are still listable, credential-editable and discoverable in the
admin UI -- only generation is missing -- so turning one on is a matter of
adding one adapter, not reworking the gateway.
"""

from __future__ import annotations

from .anthropic import AnthropicBackend
from .base import Artifact, Backend, BackendError, BackendRequest, BackendResult
from .bfl import BflBackend
from .elevenlabs import ElevenLabsBackend
from .fal import FalBackend
from .fish import FishBackend
from .kie import KieBackend
from .minimax import MiniMaxBackend
from .openai_compat import OpenAICompatibleBackend
from .pool import PoolBackend
from .studio_hub import StudioHubBackend
from .google import generate_music
from .xai import XaiBackend

from ..providers.definitions import (
    IMPLEMENTED_KINDS,
    KIND_POOL,
    KIND_ANTHROPIC,
    KIND_BFL,
    KIND_ELEVENLABS,
    KIND_FAL,
    KIND_FISH,
    KIND_GOOGLE,
    KIND_KIE,
    KIND_MINIMAX,
    KIND_OPENAI_COMPATIBLE,
    KIND_STUDIO_HUB,
    KIND_XAI,
    ProviderDefinition,
)

# Kinds with no generation adapter.
#
# Empty: every kind a provider can declare now has one. The map is kept because
# `support_status` reads it and because adding a provider without an adapter is
# a normal intermediate state — it stays discoverable and configurable, and its
# models are withheld from Story Studio rather than offered and then broken.
PENDING_KINDS: dict[str, str] = {}


class UnsupportedBackend:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def generate(self, request: BackendRequest) -> BackendResult:
        raise BackendError(self.reason, code="PROVIDER_NOT_IMPLEMENTED")


class GoogleBackend:
    """Google AI through its OpenAI-compatible surface.

    Gemini exposes `/openai/chat/completions`, so the OpenAI adapter serves it
    unchanged. Discovery still uses Google's native `/models` shape, because
    that endpoint carries the supported-generation-methods metadata the OpenAI
    surface does not.
    """

    def __init__(self) -> None:
        self._inner = OpenAICompatibleBackend()

    def generate(self, request: BackendRequest) -> BackendResult:
        # Music is the one modality the OpenAI-compatible surface cannot carry:
        # Lyria is served through Gemini's own `:generateContent`, returning inline
        # base64 audio, so it has a native path rather than a delegation.
        if request.modality == "music":
            return generate_music(request)
        base = (request.values.get("base_url") or request.base_url).rstrip("/")
        if not base.endswith("/openai"):
            base = f"{base}/openai"
        return self._inner.generate(
            BackendRequest(
                provider_id=request.provider_id,
                model_id=request.model_id,
                modality=request.modality,
                params=request.params,
                values=request.values,
                base_url=base,
                display_name=request.display_name,
            )
        )


_OPENAI_COMPATIBLE = OpenAICompatibleBackend()
_ANTHROPIC = AnthropicBackend()
_GOOGLE = GoogleBackend()
_FAL = FalBackend()
_KIE = KieBackend()
_FISH = FishBackend()
_BFL = BflBackend()
_MINIMAX = MiniMaxBackend()
_ELEVENLABS = ElevenLabsBackend()
_STUDIO_HUB = StudioHubBackend()
_XAI = XaiBackend()
_POOL = PoolBackend()

_BY_KIND: dict[str, Backend] = {
    KIND_OPENAI_COMPATIBLE: _OPENAI_COMPATIBLE,
    KIND_ANTHROPIC: _ANTHROPIC,
    KIND_GOOGLE: _GOOGLE,
    KIND_FAL: _FAL,
    KIND_KIE: _KIE,
    KIND_FISH: _FISH,
    KIND_BFL: _BFL,
    KIND_MINIMAX: _MINIMAX,
    KIND_ELEVENLABS: _ELEVENLABS,
    KIND_STUDIO_HUB: _STUDIO_HUB,
    KIND_XAI: _XAI,
    KIND_POOL: _POOL,
}


# The catalogue decides what Story Studio may see from IMPLEMENTED_KINDS; this
# module decides what can actually run from `_BY_KIND`. If those drift, the
# gateway either hides a working provider or — far worse — offers a model that
# fails on use. Fail loudly at import instead.
assert set(_BY_KIND) == set(IMPLEMENTED_KINDS), (
    "adapter/catalogue mismatch — "
    f"adapter-only: {sorted(set(_BY_KIND) - set(IMPLEMENTED_KINDS))}, "
    f"catalogue-only: {sorted(set(IMPLEMENTED_KINDS) - set(_BY_KIND))}"
)


#: Which modalities each kind's adapter *actually* serves.
#:
#: A kind having an adapter is not the same claim as that adapter serving a
#: modality, and the import-time assert above only compares kinds — so a modality
#: missing *inside* a working kind was invisible. It was not harmless:
#:
#:   * `google-ai` declares six modalities, and `GoogleBackend` forwards all of
#:     them to the OpenAI-compatible shim, which serves four. A discovered
#:     `veo*` row (mapped to video) or `lyria*` row (music) published to Story
#:     Studio and then failed on use — the exact state this codebase calls the
#:     worst one, because the picker offers it and the provider refuses it;
#:   * `kie-ai-images` declares `chat` while `KieBackend` serves image, video and
#:     music. No curated chat model exists for it, so it bit only a
#:     hand-added row — which is precisely what "add a model by hand" does.
#:     **Music is on the same `jobs/createTask` route, under a different api-model
#:     name** (`ai-music-api/generate`, with the Suno tier in `input.model`) —
#:     Kie moved it there from its old dedicated `/api/v1/generate*` family, which
#:     is why music is served here again after a spell of being withheld. Measured
#:     live 2026-09-20: the body queues, `recordInfo` reports success, and
#:     `resultJson.data[]` carries the track urls.
#:
#: `studio_hub` is accurate both ways, so locations are unaffected.
_MODALITIES_BY_KIND: dict[str, frozenset[str]] = {
    KIND_OPENAI_COMPATIBLE: frozenset({"chat", "image", "tts", "transcription"}),
    KIND_ANTHROPIC: frozenset({"chat"}),
    # The shim's four, minus transcription: that one is *measured* broken — the
    # OpenAI-compatible transcription route 404s on Google, and publishing it was
    # tried and reverted. `image` and `tts` have real handlers and are left to
    # answer for themselves.
    # Music (Lyria) is native; the rest is the OpenAI-compatible shim.
    KIND_GOOGLE: frozenset({"chat", "image", "tts", "music"}),
    KIND_FAL: frozenset({"image", "video", "tts"}),
    KIND_KIE: frozenset({"image", "video", "music"}),
    KIND_FISH: frozenset({"tts"}),
    KIND_BFL: frozenset({"image"}),
    KIND_MINIMAX: frozenset({"image", "video", "music", "tts"}),
    KIND_ELEVENLABS: frozenset({"tts"}),
    # Music is a real Hub lane (engine `music-mlx`, two engines, WAV artifacts) and
    # Video joined it on 2026-09-26, so a location's music and video rows are offerable rather
    # than published and refused. The Hub's own video models are retired (2026-10-01) and a Hub
    # now publishes none; the lane stays so a video model a Hub adds later runs without a change
    # here. With none published, Video offers only the cloud providers' models.
    KIND_STUDIO_HUB: frozenset({"chat", "image", "tts", "music", "transcription", "video"}),
    # Chat is delegated to the OpenAI shim; image and video are native.
    KIND_XAI: frozenset({"chat", "image", "video"}),
    # A pool is chat only. Its members are chat models by construction, and a
    # pool that offered another modality would be offering a route that tries
    # chat models for an image request.
    KIND_POOL: frozenset({"chat"}),
}

assert set(_MODALITIES_BY_KIND) == set(_BY_KIND), (
    "adapter/modality table mismatch — "
    f"missing: {sorted(set(_BY_KIND) - set(_MODALITIES_BY_KIND))}, "
    f"stale: {sorted(set(_MODALITIES_BY_KIND) - set(_BY_KIND))}"
)


def _resolve_member(modality: str, compound: str):
    """Resolve one pool member to (definition, model), or (None, None).

    Lazily imported: `catalog` imports this module to decide what may be
    published, so a module-level edge back would be a cycle. The lookup is the
    catalogue's own, so a member the owner has since disabled resolves to nothing
    here for exactly the reason it would be refused on its own.
    """
    from .. import catalog

    return catalog.resolve(modality, compound)


def serves(definition: ProviderDefinition, modality: str) -> bool:
    """Whether this provider can actually run this modality.

    The single gate the catalogue reads, so "there is an adapter for the kind"
    and "that adapter serves this modality" cannot drift apart again.
    """
    if definition.kind not in IMPLEMENTED_KINDS:
        return False
    served = _MODALITIES_BY_KIND.get(definition.kind)
    if served is None:
        return False
    return modality in served


def adapter_for(definition: ProviderDefinition) -> Backend:
    backend = _BY_KIND.get(definition.kind)
    if backend is not None:
        return backend
    return UnsupportedBackend(
        PENDING_KINDS.get(definition.kind, f"no adapter for kind '{definition.kind}'")
    )


def support_status(definition: ProviderDefinition, modality: str | None = None) -> tuple[bool, str]:
    """(can_generate, human explanation) for the admin UI."""
    if definition.kind in PENDING_KINDS:
        return False, PENDING_KINDS[definition.kind]
    if definition.kind not in _BY_KIND:
        return False, f"no adapter for kind '{definition.kind}'"
    if modality is not None and not serves(definition, modality):
        return False, (
            f"the {definition.kind} adapter does not serve {modality} — "
            "this model is withheld from Story Studio rather than offered and then refused"
        )
    return True, ""


__all__ = [
    "AnthropicBackend",
    "Backend",
    "BackendError",
    "BackendRequest",
    "BackendResult",
    "FalBackend",
    "GoogleBackend",
    "KieBackend",
    "OpenAICompatibleBackend",
    "PENDING_KINDS",
    "StudioHubBackend",
    "UnsupportedBackend",
    "adapter_for",
    "serves",
    "support_status",
]
# The pool adapter dispatches instead of speaking HTTP, so it needs the two
# functions this module owns -- the catalogue's resolver and this module's own
# `adapter_for`. Attached here, once both exist.
_POOL.attach(_resolve_member, adapter_for)
