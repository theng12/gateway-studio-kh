"""Provider credential storage.

Credentials live in `data/credentials.json`, mode 0600, gitignored. Each field
of each provider resolves through a three-step chain:

    value stored via the admin UI  >  value from gateway-kh/.env  >  absent

The `.env` fallback matters: it lets the owner drop keys into a file and have
them work without ever opening the UI, and it means a key never has to be typed
into a browser if that is uncomfortable.

Secrets are never returned to the browser in full. `mask()` exists for the admin
API so the UI can show *which* credentials are configured without exposing them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from ..config import CREDENTIALS_PATH, get, write_secret
from .definitions import PROVIDERS_BY_ID, ProviderDefinition


def mask(value: str) -> str:
    """Render a secret for display: enough to identify, not enough to use."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


class CredentialStore:
    def __init__(
        self,
        path: Path | None = None,
        definition_lookup: "Callable[[str], Any] | None" = None,
    ) -> None:
        self.path = Path(path or CREDENTIALS_PATH)
        self._data: dict[str, Any] = {"providers": {}}
        # How a provider id is resolved to its definition. Set by the registry,
        # because definitions are not only the built-ins: a location the owner
        # added on the Fleet page is a provider too, and its Hub token has to be
        # storable. Left as `None`, this store only knows the static providers —
        # which is what a test wants when it builds one directly.
        self._definition_lookup = definition_lookup
        self._load()

    def _definition(self, provider_id: str) -> Any:
        if self._definition_lookup is not None:
            found = self._definition_lookup(provider_id)
            if found is not None:
                return found
        return PROVIDERS_BY_ID.get(provider_id)

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt credential file must not take the gateway down; the
            # admin UI will simply show nothing configured.
            return
        if isinstance(parsed, dict) and isinstance(parsed.get("providers"), dict):
            self._data = parsed

    def _save(self) -> None:
        write_secret(self.path, json.dumps(self._data, indent=2) + "\n")

    # -- reads ------------------------------------------------------------
    def stored(self, provider_id: str) -> dict[str, str]:
        entry = self._data["providers"].get(provider_id) or {}
        values = entry.get("values")
        return dict(values) if isinstance(values, dict) else {}

    def value_for(self, provider_id: str, field_key: str) -> str:
        """Resolve one field: stored value first, then `.env`."""
        stored = self.stored(provider_id).get(field_key)
        if stored:
            return str(stored)
        definition = self._definition(provider_id)
        if definition is None:
            return ""
        for credential_field in definition.credential_fields:
            if credential_field.key == field_key:
                # Through `config.get`, which re-reads `.env` when it changes.
                # This store used to keep a **second** copy read at construction,
                # refreshed only by an explicit `reload_env()` — so the two readers
                # of one file disagreed about when an edit had happened.
                return str(get(credential_field.env_var, "") or "")
        return ""

    def resolved(self, provider_id: str) -> dict[str, str]:
        """Every field for a provider, resolved through the chain."""
        definition = self._definition(provider_id)
        if definition is None:
            return {}
        return {
            f.key: self.value_for(provider_id, f.key) for f in definition.credential_fields
        }

    def is_configured(self, provider_id: str) -> bool:
        """True when every REQUIRED field is resolvable.

        Optional fields (a base-url override, a group id) do not gate a provider.
        """
        definition = self._definition(provider_id)
        if definition is None:
            return False
        if not definition.credential_fields:
            return True
        for credential_field in definition.credential_fields:
            if credential_field.required and not self.value_for(provider_id, credential_field.key):
                return False
        return True

    def is_enabled(self, provider_id: str) -> bool:
        entry = self._data["providers"].get(provider_id) or {}
        if "enabled" in entry:
            return bool(entry["enabled"])
        definition = self._definition(provider_id)
        # The switch is independent of key readiness; serving still requires both.
        return bool(definition and definition.default_enabled)

    def source_for(self, provider_id: str, field_key: str) -> str:
        """Where a value came from: 'stored' | 'env' | 'none'. For the UI."""
        if self.stored(provider_id).get(field_key):
            return "stored"
        definition = self._definition(provider_id)
        if definition:
            for credential_field in definition.credential_fields:
                if credential_field.key == field_key and get(credential_field.env_var):
                    return "env"
        return "none"

    # -- writes -----------------------------------------------------------
    def set_values(self, provider_id: str, values: dict[str, str]) -> None:
        """Store credential values. An empty string clears a field."""
        definition = self._definition(provider_id)
        if definition is None:
            raise KeyError(provider_id)
        allowed = {f.key for f in definition.credential_fields}

        entry = self._data["providers"].setdefault(provider_id, {})
        current = dict(entry.get("values") or {})
        for key, value in values.items():
            if key not in allowed:
                continue
            if value == "":
                current.pop(key, None)
            else:
                current[key] = value
        entry["values"] = current
        entry["updated_at"] = time.time()
        # Configuring a provider for the first time should turn it on, unless
        # the owner explicitly turned it off before.
        if "enabled" not in entry and self.is_configured(provider_id):
            entry["enabled"] = definition.default_enabled
        self._save()

    def set_values_for(
        self, scope_id: str, values: dict[str, str], allowed_fields: Iterable[str]
    ) -> None:
        """Store credential values for an id that is **not** a model provider.

        A sound source is a credential *scope* — it can hold a key — but it
        contributes no catalogue rows, so it deliberately has no
        `ProviderDefinition`, and `set_values` (which resolves the allowed fields
        from one) cannot store for it. The caller passes the keys **its own
        declaration** lists, so the check is the same one `set_values` makes; only
        where the list comes from differs.

        Two differences from `set_values`, both deliberate:

        * an unknown key is **refused**, not dropped. `set_values` silently ignores
          one, which makes a typo answer "saved" while storing nothing; this door is
          new, so it can be strict from the start.
        * no `enabled` flag is written. A source is used or not by whether it has a
          credential — there is nothing to switch off, and an `enabled: false` entry
          here would make `is_enabled` answer about something that has no toggle.
        """
        allowed = {str(key) for key in allowed_fields}
        unknown = sorted(key for key in values if key not in allowed)
        if unknown:
            raise ValueError("unknown field(s): " + ", ".join(unknown))

        entry = self._data["providers"].setdefault(scope_id, {})
        current = dict(entry.get("values") or {})
        for key, value in values.items():
            if value == "":
                current.pop(key, None)
            else:
                current[key] = value
        entry["values"] = current
        entry["updated_at"] = time.time()
        self._save()

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        # Resolved through `_definition`, not the static map: a Studio Hub
        # location is derived from the fleet roster and is never in
        # `PROVIDERS_BY_ID`. The admin route checks the id with
        # `registry.definition()` — which *does* know locations — and then this
        # raised a bare `KeyError` that nothing catches, so switching one of the
        # owner's locations off answered **500**. Filthy fifth instance of the
        # same mistake, and the most likely one to be hit: the credential is
        # stored for a location the moment it is added.
        if self._definition(provider_id) is None:
            raise KeyError(provider_id)
        entry = self._data["providers"].setdefault(provider_id, {})
        entry["enabled"] = bool(enabled)
        entry["updated_at"] = time.time()
        self._save()

    def forget(self, provider_id: str) -> None:
        """Remove a provider's entry outright.

        `clear` keeps an empty entry on purpose — "this was configured, now
        cleared" is worth knowing. But a *deleted* location has no meaning left,
        and leaving its entry makes the store accumulate references to provider
        ids that no longer exist.
        """
        if self._data["providers"].pop(provider_id, None) is not None:
            self._save()

    def clear(self, provider_id: str) -> None:
        entry = self._data["providers"].get(provider_id)
        if entry is None:
            return
        entry["values"] = {}
        entry["enabled"] = False
        entry["updated_at"] = time.time()
        self._save()

    # -- projection -------------------------------------------------------
    def describe(self, definition: ProviderDefinition) -> dict[str, Any]:
        """Provider state safe to hand to the admin UI (secrets masked)."""
        resolved = self.resolved(definition.id)
        return {
            "id": definition.id,
            "display_name": definition.display_name,
            "category": definition.category,
            "kind": definition.kind,
            # Where the compute actually is. `studio_hub` is the one kind that
            # talks to a machine on the LAN rather than to an API over the
            # internet, and the two are managed differently: a cloud provider is
            # a key and a model list, while a local one is a *location* with
            # machines and its own running work. The console groups them by this
            # rather than by name, so a second Hub is classified correctly the
            # moment it is added.
            "scope": "local" if definition.kind == "studio_hub" else "cloud",
            "description": definition.description,
            "docs_url": definition.docs_url,
            "notes": definition.notes,
            "modalities": list(definition.modalities),
            "base_url": definition.base_url,
            "discovery": definition.discovery,
            "enabled": self.is_enabled(definition.id),
            "configured": self.is_configured(definition.id),
            "configured_fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "env_var": f.env_var,
                    "secret": f.secret,
                    "required": f.required,
                    "placeholder": f.placeholder,
                    "help_text": f.help_text,
                    "source": self.source_for(definition.id, f.key),
                    "present": bool(resolved.get(f.key)),
                    "masked": mask(resolved.get(f.key, "")) if f.secret else resolved.get(f.key, ""),
                }
                for f in definition.credential_fields
            ],
        }
