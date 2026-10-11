"""Provider registry — the single object the rest of the gateway talks to.

Composes the credential store and the model inventory so callers never have to
know how provider state is split across files. Everything the admin API and the
catalogue builder need is reachable from here.
"""

from __future__ import annotations

import os
from typing import Any

from .credentials import CredentialStore
from .definitions import (
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    PROVIDERS_BY_ID,
    CredentialField,
    ProviderDefinition,
)
from .discovery import DiscoveryResult
from .models import ModelInventory


#: Locations are derived from the roster rather than loaded at import, so adding
#: one on the Fleet page takes effect without restarting the gateway. The cache
#: is keyed on the roster file's mtime, so the common path is one stat.
#:
#: The roster path itself comes from `fleet.FLEET_PATH` (it honours
#: `GATEWAY_FLEET`); this module no longer keeps a copy.
_fleet_cache: dict = {"key": None, "by_id": {}}


def _fleet_definitions_by_id() -> dict[str, ProviderDefinition]:
    from ..fleet import FLEET_PATH, PROVIDER_PREFIX, revision, roster

    # Keyed on the writer's own revision counter *and* the file's mtime: the
    # counter catches our writes deterministically (an mtime can repeat within
    # one second on a coarse filesystem, which would silently drop a change),
    # and the mtime still catches an edit made outside this process.
    #
    # `FLEET_PATH` comes from `fleet`, which honours `GATEWAY_FLEET`. This module
    # used to hold its own `DATA_DIR / "fleet.json"` — so on a deployment that
    # pointed the roster elsewhere, the cache watched a file nothing wrote: a
    # location added through the app never invalidated the derived providers, and
    # the gateway kept serving the roster it had read at first use. One path, from
    # the module that owns the file.
    try:
        key = (revision(), os.path.getmtime(FLEET_PATH))
    except OSError:
        key = (revision(), None)
    if _fleet_cache["key"] == key:
        return _fleet_cache["by_id"]

    by_id: dict[str, ProviderDefinition] = {}
    for row in roster():
        controller_id = str(row.get("id") or "")
        url = str(row.get("url") or "")
        if not controller_id or not url:
            continue
        provider_id = f"{PROVIDER_PREFIX}{controller_id}"
        by_id[provider_id] = ProviderDefinition(
            id=provider_id,
            display_name=str(row.get("label") or url),
            category="multi",
            kind=KIND_STUDIO_HUB,
            base_url=url,
            # The Hub's own routes: readiness first, because standalone
            # `/api/hub/capabilities` advertises zero models (see the local
            # provider's definition).
            modalities=("chat", "image", "tts", "music", "transcription", "video", "embeddings"),
            discovery=DISCOVERY_STUDIO_HUB,
            description=f"Studio Hub controller at {url}.",
            credential_fields=(
                CredentialField(
                    key="hub_token",
                    label="Fleet token",
                    env_var=f"FLEET_{controller_id.upper()}_HUB_TOKEN",
                    secret=True,
                    placeholder="the controller's Fleet token",
                    help_text=(
                        "The work badge for the group. From the controller's Connections page, "
                        "the token labelled Fleet token. It sends work, follows the job, "
                        "downloads and acknowledges the result, cancels or discards an uncertain "
                        "one, and reads the model and fleet lists; it cannot change settings, "
                        "install or remove models, delete output, manage shared voices, or make "
                        "a Mac join or leave. This is the token GatewayKH needs to run work."
                    ),
                ),
                CredentialField(
                    key="admin_token",
                    label="Hub token (shared voices only)",
                    env_var=f"FLEET_{controller_id.upper()}_ADMIN_TOKEN",
                    secret=True,
                    required=False,
                    placeholder="the controller's Hub token",
                    help_text=(
                        "The Mac's own key. From the controller's Connections page, the token "
                        "labelled Hub token. Optional, and needed only to create, list or remove "
                        "shared voices on that controller — the Hub refuses those routes to a "
                        "fleet token on purpose, so a node's credential can never administer a "
                        "controller. Never send it unless GatewayKH will manage shared voices "
                        "there; anyone who holds it can do everything on that Mac."
                    ),
                ),
            ),
            # Opt-in, like every provider except the local Hub: adding a site
            # must not silently publish its whole model list to Story Studio.
            auto_enable_discovered=False,
            notes="Added on the Fleet page.",
        )
    _fleet_cache["key"] = key
    _fleet_cache["by_id"] = by_id
    return by_id


def _fleet_definitions() -> list[ProviderDefinition]:
    return list(_fleet_definitions_by_id().values())


def _pool_definition() -> ProviderDefinition | None:
    """The synthetic provider pools are published under, or None when there are none.

    Read through `pools.definition()`, which returns None for an empty file, so a
    gateway with no pools has no `pool` provider at all rather than an empty one
    sitting in the console.
    """
    from .. import pools

    return pools.definition()


def _lookup(provider_id: str) -> ProviderDefinition | None:
    """Resolve an id across every source of providers.

    One function for all three lookups below. They were separate lambdas naming
    only the fleet, which is exactly how the pool provider would have ended up
    resolvable by the catalogue and invisible to the credential store.
    """
    found = PROVIDERS_BY_ID.get(provider_id) or _fleet_definitions_by_id().get(provider_id)
    if found is not None:
        return found
    pool = _pool_definition()
    return pool if pool is not None and pool.id == provider_id else None


class ProviderRegistry:
    def __init__(self) -> None:
        # The store resolves ids through the registry, so a location added on
        # the Fleet page can hold a token like any other provider. The closure is
        # lazy: it runs per call, long after __init__ has finished.
        self.credentials = CredentialStore(
            definition_lookup=_lookup
        )
        self.models = ModelInventory(
            self.credentials,
            definition_lookup=_lookup,
            definitions_lookup=self.definitions,
        )

    # -- reads ------------------------------------------------------------
    def definitions(self) -> list[ProviderDefinition]:
        """Built-ins, user-defined providers, and every configured location.

        A location the owner added on the Fleet page is a **provider** here, not
        just a dashboard row: its models are discovered and its work is
        dispatched through the same `studio_hub` adapter as the local Hub, which
        already takes a per-provider base URL and token. Without this the fleet
        page could show a site that could never actually be used.
        """
        pool = _pool_definition()
        return list(PROVIDERS_BY_ID.values()) + _fleet_definitions() + ([pool] if pool else [])

    def definition(self, provider_id: str) -> ProviderDefinition | None:
        return _lookup(provider_id)

    def describe_all(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for definition in self.definitions():
            item = self.credentials.describe(definition)
            cached = self.models.cached(definition.id)
            models = self.models.models_for(definition.id, include_disabled=True)
            item["model_count"] = len(models)
            item["enabled_model_count"] = sum(1 for m in models if m["enabled"])
            # What the console must show, because "ticked" and "can be picked" are
            # different questions: `can_publish` withholds a model the Hub does not
            # describe as a model, or one it says does not fit this machine. A badge
            # counting the enabled ones read "9/40 live" for a provider whose only
            # publishable row was one model.
            item["publishable_model_count"] = sum(
                1 for m in models if self.models.can_publish(definition, m)
            )
            item["last_refresh"] = cached.get("fetched_at")
            item["last_error"] = cached.get("error")
            # **What the provider listed that this gateway could not place.**
            # Published so "it has no models" and "I did not understand what it
            # sent" cannot render the same: a Studio Hub serving a lane this
            # gateway has never heard of used to discover zero models in silence,
            # with nothing anywhere naming the engine.
            item["skipped"] = list(cached.get("skipped") or [])
            output.append(item)
        return output

    def provider_counts(self) -> dict[str, int]:
        """`total / configured / enabled` for the health and status headers.

        Counts only: `describe_all()` also builds every provider's model list, which `/health`
        (polled every 3 s by the menu bar) never reads.
        """
        defs = self.definitions()
        return {
            "total": len(defs),
            "configured": sum(1 for d in defs if self.credentials.is_configured(d.id)),
            "enabled": sum(1 for d in defs if self.credentials.is_enabled(d.id)),
        }

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

    def refresh(self, provider_id: str, *, hub_version: str | None = None) -> DiscoveryResult:
        return self.models.refresh(provider_id, hub_version=hub_version)

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
