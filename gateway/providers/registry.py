"""Provider registry — the single object the rest of the gateway talks to.

Composes the credential store and the model inventory so callers never have to
know how provider state is split across files. Everything the admin API and the
catalogue builder need is reachable from here.
"""

from __future__ import annotations

from typing import Any

from .credentials import CredentialStore
from .definitions import PROVIDERS_BY_ID, ProviderDefinition
from .discovery import DiscoveryResult
from .models import ModelInventory


class ProviderRegistry:
    def __init__(self) -> None:
        self.credentials = CredentialStore()
        self.models = ModelInventory(self.credentials)

    # -- reads ------------------------------------------------------------
    def definitions(self) -> list[ProviderDefinition]:
        return list(PROVIDERS_BY_ID.values())

    def definition(self, provider_id: str) -> ProviderDefinition | None:
        return PROVIDERS_BY_ID.get(provider_id)

    def describe_all(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for definition in self.definitions():
            item = self.credentials.describe(definition)
            cached = self.models.cached(definition.id)
            models = self.models.models_for(definition.id, include_disabled=True)
            item["model_count"] = len(models)
            item["enabled_model_count"] = sum(1 for m in models if m["enabled"])
            item["last_refresh"] = cached.get("fetched_at")
            item["last_error"] = cached.get("error")
            output.append(item)
        return output

    def describe(self, provider_id: str) -> dict[str, Any] | None:
        for item in self.describe_all():
            if item["id"] == provider_id:
                return item
        return None

    def active_providers(self) -> list[ProviderDefinition]:
        """Configured, enabled providers — the ones that can serve traffic."""
        return [
            d
            for d in self.definitions()
            if self.credentials.is_enabled(d.id) and self.credentials.is_configured(d.id)
        ]

    # -- writes -----------------------------------------------------------
    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        self.credentials.set_enabled(provider_id, enabled)

    def set_credentials(self, provider_id: str, values: dict[str, str]) -> None:
        self.credentials.set_values(provider_id, values)

    def clear_credentials(self, provider_id: str) -> None:
        self.credentials.clear(provider_id)

    def refresh(self, provider_id: str) -> DiscoveryResult:
        return self.models.refresh(provider_id)

    def reload_env(self) -> None:
        """Pick up `.env` edits without a restart."""
        self.credentials.reload_env()

    # -- dependency -------------------------------------------------------
    def resolver(self, modality: str, model_id: str):
        """Find the (provider, model) pair that should serve a request.

        Resolution is scoped to enabled providers and enabled models, so a
        disabled toggle is honoured on the generation path and not just in the
        catalogue — the two must never disagree.
        """
        for definition, model in self.models.enabled_models(modality):
            if model["model_id"] == model_id:
                return definition, model
        return None, None


registry = ProviderRegistry()
