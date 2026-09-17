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


def adapter_for(definition: ProviderDefinition) -> Backend:
    backend = _BY_KIND.get(definition.kind)
    if backend is not None:
        return backend
    return UnsupportedBackend(
        PENDING_KINDS.get(definition.kind, f"no adapter for kind '{definition.kind}'")
    )


def support_status(definition: ProviderDefinition) -> tuple[bool, str]:
    """(can_generate, human explanation) for the admin UI."""
    if definition.kind in PENDING_KINDS:
        return False, PENDING_KINDS[definition.kind]
    if definition.kind not in _BY_KIND:
        return False, f"no adapter for kind '{definition.kind}'"
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
    "support_status",
]
