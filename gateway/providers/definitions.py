"""Provider definitions — the declarative core of the gateway.

Everything provider-specific lives here as data, not as bespoke code. Adding a
new provider means adding one `ProviderDefinition`; adding a new model to an
existing provider means either letting discovery find it, or adding one static
entry to that provider's `static_models`.

This mirrors the 17 cloud providers GenStudio KH already had configured, plus
the local Studio Hub lane. The ids are kept identical to GenStudio's so that
credentials and mental models transfer directly.

Design rules:

  * `kind` selects the adapter implementation (how to talk to the provider).
  * `discovery` selects how its model list is obtained.
  * `modalities` gates which Story Studio catalogues a provider can contribute to.
  * `credential_fields` drives the admin UI, so adding a field never requires
    touching frontend code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import DATA_DIR

# ---------------------------------------------------------------------------
# Adapter kinds
# ---------------------------------------------------------------------------
KIND_OPENAI_COMPATIBLE = "openai_compatible"
KIND_ANTHROPIC = "anthropic"
KIND_GOOGLE = "google"
KIND_FAL = "fal"
KIND_KIE = "kie"
KIND_BFL = "bfl"
KIND_MINIMAX = "minimax"
KIND_ELEVENLABS = "elevenlabs"
KIND_FISH = "fish"
KIND_STUDIO_HUB = "studio_hub"

#: Kinds that actually have a generation adapter.
#:
#: This is the single source of truth, and it is deliberately declared here
#: rather than discovered from `gateway.adapters` — `providers` must not depend
#: on `adapters`, and more importantly the catalogue needs to know *before*
#: offering a model whether it can be generated. A provider whose kind is
#: missing from this set is discoverable and configurable, but its models are
#: withheld from Story Studio: showing a model that fails on use is worse than
#: not showing it.
IMPLEMENTED_KINDS: frozenset[str] = frozenset(
    {
        KIND_OPENAI_COMPATIBLE,
        KIND_ANTHROPIC,
        KIND_GOOGLE,
        KIND_FAL,
        KIND_KIE,
        KIND_FISH,
        KIND_STUDIO_HUB,
        KIND_BFL,
        KIND_MINIMAX,
        KIND_ELEVENLABS,
    }
)

# ---------------------------------------------------------------------------
# Discovery strategies
# ---------------------------------------------------------------------------
DISCOVERY_OPENAI_MODELS = "openai_models"      # GET {base}/models  (OpenAI-compatible)
DISCOVERY_ANTHROPIC_MODELS = "anthropic_models"  # GET {base}/models
DISCOVERY_GOOGLE_MODELS = "google_models"      # GET {base}/models?key=
DISCOVERY_FAL_CATALOG = "fal_catalog"          # GET api.fal.ai/v1/models
DISCOVERY_ELEVENLABS = "elevenlabs_models"     # GET {base}/v1/models
DISCOVERY_STUDIO_HUB = "studio_hub"            # GET {hub}/api/hub/model-packages
DISCOVERY_STATIC = "static"                    # curated list only (doc-driven providers)


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str
    label: str
    env_var: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    id: str
    display_name: str
    category: str          # language | image | voice | video | music | multi
    kind: str
    base_url: str
    modalities: tuple[str, ...]      # chat | image | video | tts | transcription | music
    discovery: str
    description: str = ""
    docs_url: str = ""
    # Extra host used only for listing models, when the generation host differs
    # (varies by provider: fal runs jobs on queue.fal.run but lists on api.fal.ai).
    catalog_base_url: str = ""
    credential_fields: tuple[CredentialField, ...] = ()
    static_models: tuple[dict, ...] = ()
    default_enabled: bool = True
    #: Whether newly discovered models become available to Story Studio
    #: immediately.
    #:
    #: **False by default, deliberately.** Discovery and availability are
    #: different things: a provider publishing 300 models should not put 300
    #: rows into Story Studio's picker. Models are discovered here, then
    #: explicitly enabled in the admin console — the same model GenStudio used.
    #:
    #: The local Studio Hub provider opts back in (see its definition): those
    #: are the owner's own runners, there are about a dozen, and the local lane
    #: is the point of the thing.
    auto_enable_discovered: bool = False
    notes: str = ""

    @property
    def primary_field(self) -> str:
        return self.credential_fields[0].key if self.credential_fields else "api_key"


def _api_key(env: str, label: str = "API key", **kw) -> CredentialField:
    # setdefault, not assignment: callers legitimately override the placeholder,
    # and passing it both ways is a TypeError.
    kw.setdefault(
        "placeholder",
        "sk-…" if ("OPENAI" in env or "DEEPSEEK" in env) else "",
    )
    return CredentialField(key="api_key", label=label, env_var=env, **kw)


# When the generation host and the catalogue host differ, this is the one that
# answers "what models exist". Only fal currently needs it.
FAL_QUEUE = "https://queue.fal.run"
FAL_CATALOG = "https://api.fal.ai/v1"

PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    # ------------------------------------------------------------------ language
    ProviderDefinition(
        id="openrouter-main",
        display_name="OpenRouter",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://openrouter.ai/api/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Aggregator. One key reaches Claude, GPT, Gemini, DeepSeek and many more.",
        docs_url="https://openrouter.ai/docs",
        credential_fields=(_api_key("OPENROUTER_API_KEY"),),
    ),
    ProviderDefinition(
        id="deepseek-main",
        display_name="DeepSeek",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.deepseek.com",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="DeepSeek direct.",
        docs_url="https://api-docs.deepseek.com",
        credential_fields=(_api_key("DEEPSEEK_API_KEY"),),
    ),
    ProviderDefinition(
        id="anthropic-direct",
        display_name="Anthropic",
        category="language",
        kind=KIND_ANTHROPIC,
        base_url="https://api.anthropic.com/v1",
        modalities=("chat",),
        discovery=DISCOVERY_ANTHROPIC_MODELS,
        description="Claude direct, via the Messages API.",
        docs_url="https://docs.anthropic.com",
        credential_fields=(_api_key("ANTHROPIC_API_KEY", placeholder="sk-ant-…"),),
    ),
    ProviderDefinition(
        id="openai-direct",
        display_name="OpenAI",
        category="multi",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.openai.com/v1",
        modalities=("chat", "image", "tts", "transcription"),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="GPT chat, image generation, speech and transcription.",
        docs_url="https://platform.openai.com/docs",
        credential_fields=(_api_key("OPENAI_API_KEY"),),
    ),
    ProviderDefinition(
        id="google-ai",
        display_name="Google AI",
        category="multi",
        kind=KIND_GOOGLE,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        modalities=("chat", "image", "video", "tts", "transcription", "music"),
        discovery=DISCOVERY_GOOGLE_MODELS,
        description="Gemini chat, Imagen images, Veo video, TTS, music.",
        docs_url="https://ai.google.dev/gemini-api/docs/models",
        credential_fields=(_api_key("GOOGLE_AI_API_KEY", label="API key (paid tier)"),),
    ),
    ProviderDefinition(
        id="google-ai-free",
        display_name="Google AI (free tier)",
        category="language",
        kind=KIND_GOOGLE,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        modalities=("chat",),
        discovery=DISCOVERY_GOOGLE_MODELS,
        description="Separate free-tier key, ranked against the paid Google route.",
        docs_url="https://ai.google.dev/gemini-api/docs/models",
        credential_fields=(_api_key("GOOGLE_AI_FREE_API_KEY", label="API key (free tier)"),),
    ),
    ProviderDefinition(
        id="groq-main",
        display_name="Groq",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.groq.com/openai/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Very fast open-model inference.",
        docs_url="https://console.groq.com/docs",
        credential_fields=(_api_key("GROQ_API_KEY", placeholder="gsk_…"),),
    ),
    ProviderDefinition(
        id="nvidia-nim-main",
        display_name="NVIDIA NIM",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://integrate.api.nvidia.com/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="NVIDIA-hosted open models.",
        docs_url="https://build.nvidia.com",
        credential_fields=(_api_key("NVIDIA_NIM_API_KEY", placeholder="nvapi-…"),),
    ),
    ProviderDefinition(
        id="xai-direct",
        display_name="xAI",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.x.ai/v1",
        modalities=("chat", "image"),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Grok chat and image generation.",
        docs_url="https://docs.x.ai",
        credential_fields=(_api_key("XAI_API_KEY", placeholder="xai-…"),),
    ),
    ProviderDefinition(
        id="tokenharbor",
        display_name="Token Harbor",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://tokenharbor.ai/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Free language routes. Requires dashboard opt-in; may retain prompts.",
        docs_url="https://tokenharbor.ai",
        credential_fields=(_api_key("TOKENHARBOR_API_KEY"),),
        notes="Free routes may retain prompts and responses. Treat as non-private.",
    ),
    ProviderDefinition(
        id="experiential-labs",
        display_name="Experiential Labs",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.experientiallabs.ai/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="OpenAI-compatible gateway with a public model catalogue.",
        docs_url="https://platform.experientiallabs.ai/docs",
        credential_fields=(_api_key("EXPERIENTIAL_LABS_API_KEY"),),
    ),

    # ----------------------------------------------------------- image / video
    ProviderDefinition(
        id="fal-ai",
        display_name="fal.ai",
        category="multi",
        kind=KIND_FAL,
        base_url=FAL_QUEUE,
        catalog_base_url=FAL_CATALOG,
        modalities=("image", "video"),
        discovery=DISCOVERY_FAL_CATALOG,
        description="Image and video aggregator with a live catalogue API.",
        docs_url="https://fal.ai/models",
        credential_fields=(
            CredentialField(
                key="api_key", label="API key", env_var="FAL_API_KEY",
                placeholder="key-id:key-secret",
                help_text="fal keys are a key id and secret joined by a colon.",
            ),
        ),
    ),
    ProviderDefinition(
        id="kie-ai-images",
        display_name="Kie.ai",
        category="multi",
        kind=KIND_KIE,
        base_url="https://api.kie.ai",
        modalities=("image", "video", "chat", "music"),
        discovery=DISCOVERY_STATIC,
        description="Image, video and music aggregator. Model list is curated: Kie publishes no catalogue endpoint.",
        docs_url="https://docs.kie.ai",
        credential_fields=(_api_key("KIE_AI_API_KEY"),),
        static_models=(
            # Kie publishes no catalogue endpoint, so this list is curated from
            # Kie's own documented model ids (GenStudio's verified
            # `docs/providers/kie-ai.md` tables). These are the exact strings
            # Kie's API expects in `model` -- deriving them from documentation
            # URLs produces ids the API rejects.
            #
            # Image
            {"id": "gpt-image-2-text-to-image", "display_name": "GPT Image 2", "modality": "image"},
            {"id": "gpt-image-2-image-to-image", "display_name": "GPT Image 2 (edit)", "modality": "image"},
            {"id": "nano-banana-2", "display_name": "Nano Banana 2", "modality": "image"},
            {"id": "nano-banana-2-lite", "display_name": "Nano Banana 2 Lite", "modality": "image"},
            {"id": "gemini-3-pro-image", "display_name": "Gemini 3 Pro Image", "modality": "image"},
            {"id": "seedream/5-pro-text-to-image", "display_name": "Seedream 5 Pro", "modality": "image"},
            {"id": "seedream/5-lite-text-to-image", "display_name": "Seedream 5 Lite", "modality": "image"},
            {"id": "qwen2/text-to-image", "display_name": "Qwen2 Image", "modality": "image"},
            {"id": "z-image", "display_name": "Z-Image", "modality": "image"},
            {"id": "wan/2-7-image", "display_name": "WAN 2.7 Image", "modality": "image"},
            {"id": "flux-2/pro-text-to-image", "display_name": "FLUX 2 Pro", "modality": "image"},
            {"id": "flux-2/pro-image-to-image", "display_name": "FLUX 2 Pro (edit)", "modality": "image"},
            {"id": "flux-2/flex-text-to-image", "display_name": "FLUX 2 Flex", "modality": "image"},
            {"id": "flux-2/flex-image-to-image", "display_name": "FLUX 2 Flex (edit)", "modality": "image"},
            # Video
            {"id": "happyhorse-1-1/text-to-video", "display_name": "HappyHorse 1.1", "modality": "video"},
            {"id": "happyhorse-1-1/image-to-video", "display_name": "HappyHorse 1.1 (image)", "modality": "video"},
            {"id": "kling/v3-turbo-text-to-video", "display_name": "Kling 3.0 Turbo", "modality": "video"},
            {"id": "kling/v3-turbo-image-to-video", "display_name": "Kling 3.0 Turbo (image)", "modality": "video"},
            {"id": "pixverse-v6/text-to-video", "display_name": "PixVerse V6", "modality": "video"},
            {"id": "bytedance/seedance-2-mini", "display_name": "Seedance 2.0 Mini", "modality": "video"},
            {"id": "grok-imagine-video-1-5-preview", "display_name": "Grok Imagine Video 1.5", "modality": "video"},
            {"id": "gemini-omni-video", "display_name": "Gemini Omni Video", "modality": "video"},
            # Music
            {"id": "V6", "display_name": "Suno V6", "modality": "music"},
        ),
        notes="Kie publishes no model catalogue endpoint. This list is curated from Kie's documented model ids; add new ones here or via Add model manually.",
    ),
    ProviderDefinition(
        id="bfl-direct",
        display_name="Black Forest Labs",
        category="image",
        kind=KIND_BFL,
        base_url="https://api.bfl.ai",
        modalities=("image",),
        discovery=DISCOVERY_STATIC,
        description="FLUX direct from the model authors.",
        docs_url="https://docs.bfl.ai",
        credential_fields=(_api_key("BFL_API_KEY"),),
        static_models=(
            {"id": "flux-pro-1.1", "display_name": "FLUX 1.1 Pro", "modality": "image"},
            {"id": "flux-pro", "display_name": "FLUX Pro", "modality": "image"},
            {"id": "flux-dev", "display_name": "FLUX Dev", "modality": "image"},
            {"id": "flux-kontext-pro", "display_name": "FLUX Kontext Pro", "modality": "image"},
        ),
    ),
    ProviderDefinition(
        id="minimax-direct",
        display_name="MiniMax",
        category="multi",
        kind=KIND_MINIMAX,
        base_url="https://api.minimax.io",
        modalities=("image", "video", "tts", "music"),
        discovery=DISCOVERY_STATIC,
        description="Image, video, speech and music.",
        docs_url="https://platform.minimaxi.com/document",
        credential_fields=(
            _api_key("MINIMAX_API_KEY", label="API key"),
            CredentialField(
                key="group_id", label="Group ID", env_var="MINIMAX_GROUP_ID",
                secret=False, required=False,
                help_text="Required by some MiniMax endpoints.",
            ),
        ),
        static_models=(
            {"id": "image-01", "display_name": "MiniMax Image-01", "modality": "image"},
            {"id": "video-01", "display_name": "MiniMax Video-01", "modality": "video"},
            {"id": "music-01", "display_name": "MiniMax Music-01", "modality": "music"},
            {"id": "speech-2.8-turbo", "display_name": "MiniMax Speech 2.8 Turbo", "modality": "tts"},
            {"id": "speech-2.8-hd", "display_name": "MiniMax Speech 2.8 HD", "modality": "tts"},
        ),
    ),

    # -------------------------------------------------------------------- voice
    ProviderDefinition(
        id="elevenlabs-direct",
        display_name="ElevenLabs",
        category="voice",
        kind=KIND_ELEVENLABS,
        base_url="https://api.elevenlabs.io",
        modalities=("tts",),
        discovery=DISCOVERY_ELEVENLABS,
        description="Speech synthesis and a shared voice library.",
        docs_url="https://elevenlabs.io/docs",
        credential_fields=(_api_key("ELEVENLABS_API_KEY"),),
    ),
    ProviderDefinition(
        id="fish-audio",
        display_name="Fish Audio",
        category="voice",
        kind=KIND_FISH,
        base_url="https://api.fish.audio",
        modalities=("tts",),
        discovery=DISCOVERY_STATIC,
        description="Private voice cloning and S2.1 speech.",
        docs_url="https://docs.fish.audio",
        credential_fields=(_api_key("FISH_AUDIO_API_KEY"),),
        static_models=(
            # Fish's documented `model` header values. The free tier runs the
            # SAME S2.1 Pro model as paid -- `s2.1-pro-free` is a real,
            # first-class model string, not a trial shim, and it is simply not
            # listed in the API reference table. Do not remove it for being
            # absent from the docs.
            {"id": "s2.1-pro-free", "display_name": "Fish S2.1 Pro (free)", "modality": "tts"},
            {"id": "s2.1-pro", "display_name": "Fish S2.1 Pro", "modality": "tts"},
            {"id": "s2-pro", "display_name": "Fish S2 Pro", "modality": "tts"},
            {"id": "s1", "display_name": "Fish S1", "modality": "tts"},
        ),
        notes="Free tier (s2.1-pro-free) runs the same S2.1 Pro model as paid, under a Fair Use Policy.",
    ),

    # --------------------------------------------------------------------- local
    ProviderDefinition(
        id="studio-hub-local",
        display_name="Studio Hub (this Mac)",
        category="multi",
        kind=KIND_STUDIO_HUB,
        base_url="",  # discovered at runtime from hub-ready.json
        modalities=("image", "tts", "transcription"),
        discovery=DISCOVERY_STUDIO_HUB,
        description="Your own Apple Silicon runners: FLUX images, Qwen3/VoxCPM2/OmniVoice voice, Whisper transcription.",
        docs_url="",
        credential_fields=(
            CredentialField(
                key="hub_token", label="Hub token", env_var="STUDIOHUB_HUB_TOKEN",
                required=False,
                help_text="Read from the Hub's .hub_token file. Loopback Hub is passwordless unless STUDIOHUB_REQUIRE_AUTH=1.",
            ),
            CredentialField(
                key="base_url", label="Hub base URL", env_var="STUDIOHUB_BASE_URL",
                secret=False, required=False, placeholder="auto-discovered from hub-ready.json",
            ),
        ),
        # The one provider that opts back in to auto-enable: these are the
        # owner's own local runners, there are about a dozen, and requiring a
        # click for each would be friction for no benefit.
        auto_enable_discovered=True,
        notes="Local execution. Story Studio is contractually forbidden from calling this directly; the gateway is the sanctioned path.",
    ),
)

PROVIDERS_BY_ID: dict[str, ProviderDefinition] = {p.id: p for p in PROVIDER_DEFINITIONS}


# ---------------------------------------------------------------------------
# User-defined providers
#
# The built-in list covers everything the owner already had in GenStudio, but a
# gateway that can only ever speak to 18 hard-coded vendors is not modular. This
# reads `data/providers.local.json` at import time and merges whatever it finds.
#
# Loaded at import, so adding one needs a restart. That is a deliberate
# trade-off: provider definitions are structural (they select an adapter), not
# runtime state, and keeping them static means the catalogue cannot change shape
# underneath an in-flight job.
# ---------------------------------------------------------------------------
CUSTOM_PROVIDERS_PATH = DATA_DIR / "providers.local.json"


def _load_custom_providers() -> tuple[ProviderDefinition, ...]:
    if not CUSTOM_PROVIDERS_PATH.exists():
        return ()
    try:
        raw = json.loads(CUSTOM_PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    entries = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return ()

    loaded: list[ProviderDefinition] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        provider_id = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        if not provider_id or provider_id in PROVIDERS_BY_ID:
            # Never let a local file silently redefine a built-in provider.
            continue
        if kind not in _VALID_KINDS:
            continue
        fields = tuple(
            CredentialField(
                key=str(f.get("key", "api_key")),
                label=str(f.get("label", "API key")),
                env_var=str(f.get("env_var") or f"CUSTOM_{provider_id.upper().replace('-', '_')}_KEY"),
                secret=bool(f.get("secret", True)),
                required=bool(f.get("required", True)),
                placeholder=str(f.get("placeholder", "")),
                help_text=str(f.get("help_text", "")),
            )
            for f in (entry.get("credential_fields") or [{"key": "api_key", "label": "API key"}])
        )
        loaded.append(
            ProviderDefinition(
                id=provider_id,
                display_name=str(entry.get("display_name") or provider_id),
                category=str(entry.get("category") or "custom"),
                kind=kind,
                base_url=str(entry.get("base_url") or ""),
                catalog_base_url=str(entry.get("catalog_base_url") or ""),
                modalities=tuple(entry.get("modalities") or ("chat",)),
                discovery=str(entry.get("discovery") or DISCOVERY_OPENAI_MODELS),
                description=str(entry.get("description") or "User-defined provider."),
                docs_url=str(entry.get("docs_url") or ""),
                credential_fields=fields,
                static_models=tuple(entry.get("static_models") or ()),
                default_enabled=bool(entry.get("default_enabled", True)),
                # Must match ProviderDefinition's own default. Defaulting to
                # True here silently undid the opt-in policy for every
                # user-defined provider.
                auto_enable_discovered=bool(entry.get("auto_enable_discovered", False)),
                notes=str(entry.get("notes") or "User-defined."),
            )
        )
    return tuple(loaded)


_VALID_KINDS = {
    KIND_OPENAI_COMPATIBLE,
    KIND_ANTHROPIC,
    KIND_GOOGLE,
    KIND_FAL,
    KIND_KIE,
    KIND_BFL,
    KIND_MINIMAX,
    KIND_ELEVENLABS,
    KIND_FISH,
    KIND_STUDIO_HUB,
}

for _custom in _load_custom_providers():
    PROVIDERS_BY_ID[_custom.id] = _custom

ALL_PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = tuple(PROVIDERS_BY_ID.values())


def providers_for_modality(modality: str) -> tuple[ProviderDefinition, ...]:
    return tuple(p for p in ALL_PROVIDER_DEFINITIONS if modality in p.modalities)
