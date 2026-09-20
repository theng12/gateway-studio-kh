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
from .base import Backend, BackendError, BackendRequest, BackendResult
from .bfl import BflBackend
from .elevenlabs import ElevenLabsBackend
from .fal import FalBackend
from .fish import FishBackend
from .kie import KieBackend
from .minimax import MiniMaxBackend
from .openai_compat import OpenAICompatibleBackend
from .studio_hub import StudioHubBackend
from .google import generate_music
from .xai import XaiBackend

from ..providers.definitions import (
    IMPLEMENTED_KINDS,
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
#:   * `kie-ai-images` declares `chat` while `KieBackend` serves image and video
#:     only. No curated chat model exists for it, so it bit only a
#:     hand-added row — which is precisely what "add a model by hand" does.
#:     **Music was the other half of that, and it was worse**: a curated Suno
#:     `V6` row went out, and `KieBackend` posted it down the generic
#:     `jobs/createTask` route — but Kie's Suno API is a *dedicated* family
#:     (`/api/v1/generate*`, a flat `{model, prompt, customMode, instrumental,
#:     callBackUrl}` body), and its own docs separate it from the Market
#:     `createTask` models entirely. A `createTask` body is not that shape, so the
#:     row could only fail. Music is gone from this kind, the definition and the
#:     row; the adapter refuses it by name.
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
    KIND_KIE: frozenset({"image", "video"}),
    KIND_FISH: frozenset({"tts"}),
    KIND_BFL: frozenset({"image"}),
    KIND_MINIMAX: frozenset({"image", "video", "music", "tts"}),
    KIND_ELEVENLABS: frozenset({"tts"}),
    KIND_STUDIO_HUB: frozenset({"image", "tts", "transcription"}),
    # Chat is delegated to the OpenAI shim; image and video are native.
    KIND_XAI: frozenset({"chat", "image", "video"}),
}

assert set(_MODALITIES_BY_KIND) == set(_BY_KIND), (
    "adapter/modality table mismatch — "
    f"missing: {sorted(set(_BY_KIND) - set(_MODALITIES_BY_KIND))}, "
    f"stale: {sorted(set(_MODALITIES_BY_KIND) - set(_BY_KIND))}"
)


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
