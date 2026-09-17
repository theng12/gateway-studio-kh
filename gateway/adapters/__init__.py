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
from .fal import FalBackend
from .fish import FishBackend
from .kie import KieBackend
from .openai_compat import OpenAICompatibleBackend
from .studio_hub import StudioHubBackend

from ..providers.definitions import (
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

# Kinds with no generation adapter yet. Kept explicit so the admin UI can say
# so up front instead of the owner discovering it at generation time.
PENDING_KINDS: dict[str, str] = {
    KIND_BFL: "Black Forest Labs generation adapter is not written yet.",
    KIND_MINIMAX: "MiniMax generation adapter is not written yet.",
    KIND_ELEVENLABS: "ElevenLabs generation adapter is not written yet.",
}


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
_STUDIO_HUB = StudioHubBackend()

_BY_KIND: dict[str, Backend] = {
    KIND_OPENAI_COMPATIBLE: _OPENAI_COMPATIBLE,
    KIND_ANTHROPIC: _ANTHROPIC,
    KIND_GOOGLE: _GOOGLE,
    KIND_FAL: _FAL,
    KIND_KIE: _KIE,
    KIND_FISH: _FISH,
    KIND_STUDIO_HUB: _STUDIO_HUB,
}


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
