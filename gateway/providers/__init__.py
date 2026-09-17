"""Provider subsystem: definitions, credentials, discovery and inventory."""

from .credentials import CredentialStore, mask
from .definitions import (
    PROVIDER_DEFINITIONS,
    PROVIDERS_BY_ID,
    ProviderDefinition,
    providers_for_modality,
)
from .discovery import DiscoveredModel, DiscoveryResult, discover, infer_modality
from .models import ModelInventory
from .registry import ProviderRegistry, registry

__all__ = [
    "CredentialStore",
    "DiscoveredModel",
    "DiscoveryResult",
    "ModelInventory",
    "PROVIDER_DEFINITIONS",
    "PROVIDERS_BY_ID",
    "ProviderDefinition",
    "ProviderRegistry",
    "discover",
    "infer_modality",
    "mask",
    "providers_for_modality",
    "registry",
]
