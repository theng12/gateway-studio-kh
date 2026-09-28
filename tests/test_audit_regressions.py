#!/usr/bin/env python3
"""Small regression checks for the two audit defects selected during the audit.

Run with::

    .venv/bin/python tests/test_audit_regressions.py

The gateway modules are imported only after the temporary runtime environment is
installed, so this file cannot inherit the owner's data, credentials or Hub file.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


class AuditRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="gateway-audit-regressions-")
        tmp = Path(cls._tmp.name)
        cls._env = patch.dict(
            os.environ,
            {
                "GATEWAY_DATA": str(tmp / "data"),
                "GATEWAY_ARTIFACTS": str(tmp / "data" / "artifacts"),
                "GATEWAY_DB": str(tmp / "data" / "jobs.sqlite3"),
                "GATEWAY_CREDENTIALS": str(tmp / "data" / "credentials.json"),
                "GATEWAY_MODEL_CACHE": str(tmp / "data" / "models_cache.json"),
                "GATEWAY_APP_SUPPORT": str(tmp / "app-support"),
                "GATEWAY_ENV_FILE": str(tmp / ".env"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(tmp / "hub-ready.json"),
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_API_KEY": "",
            },
        )
        cls._env.start()
        sys.path.insert(0, str(ROOT))

        # These imports must remain below the environment setup. Importing the
        # runtime resolves paths and constructs the process-wide store/worker.
        from fastapi import Request
        from gateway import admin, api
        from gateway.admin import AdminError
        from gateway.api import GatewayError
        from gateway.providers.credentials import CredentialStore
        from gateway.providers.definitions import (
            CredentialField,
            DISCOVERY_STATIC,
            KIND_OPENAI_COMPATIBLE,
            PROVIDERS_BY_ID,
            ProviderDefinition,
        )
        from gateway.providers.models import ModelInventory

        cls.api = api
        cls.admin = admin
        cls.AdminError = AdminError
        cls.GatewayError = GatewayError
        cls.Request = Request
        cls.CredentialStore = CredentialStore
        cls.CredentialField = CredentialField
        cls.DISCOVERY_STATIC = DISCOVERY_STATIC
        cls.KIND_OPENAI_COMPATIBLE = KIND_OPENAI_COMPATIBLE
        cls.PROVIDERS_BY_ID = PROVIDERS_BY_ID
        cls.ProviderDefinition = ProviderDefinition
        cls.ModelInventory = ModelInventory
        cls._tmp_path = tmp

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api.store.close()
        sys.path.remove(str(ROOT))
        cls._env.stop()
        cls._tmp.cleanup()

    def _request(self) -> object:
        return self.Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/status",
                "headers": [],
                "query_string": b"",
                "server": ("127.0.0.1", 8000),
                "client": ("127.0.0.1", 1),
                "scheme": "http",
            }
        )

    def _auth_helpers(self):
        return (
            ("story", lambda value: self.api.require_auth(self._request(), value)),
            ("admin", lambda value: self.admin.require_admin(self._request(), value)),
        )

    def test_story_auth_requires_key_when_bound_off_loopback(self) -> None:
        with patch.object(self.api, "is_loopback", return_value=False), patch.dict(
            os.environ, {"GATEWAY_API_KEY": ""}
        ):
            for label, helper in self._auth_helpers()[:1]:
                for authorization in (None, "", "Bearer ", "Bearer arbitrary"):
                    with self.subTest(surface=label, authorization=authorization):
                        with self.assertRaises(self.GatewayError):
                            helper(authorization)

    def test_admin_auth_requires_key_when_bound_off_loopback(self) -> None:
        with patch.object(self.admin, "is_loopback", return_value=False), patch.dict(
            os.environ, {"GATEWAY_API_KEY": ""}
        ):
            for label, helper in self._auth_helpers()[1:]:
                for authorization in (None, "", "Bearer ", "Bearer arbitrary"):
                    with self.subTest(surface=label, authorization=authorization):
                        with self.assertRaises(self.AdminError):
                            helper(authorization)

    def test_both_auth_surfaces_accept_the_configured_key_off_loopback(self) -> None:
        with patch.object(self.api, "is_loopback", return_value=False), patch.object(
            self.admin, "is_loopback", return_value=False
        ), patch.dict(os.environ, {"GATEWAY_API_KEY": "expected-key"}):
            for label, helper in self._auth_helpers():
                with self.subTest(surface=label):
                    helper("Bearer expected-key")

    def test_both_auth_surfaces_reject_a_wrong_key_off_loopback(self) -> None:
        with patch.object(self.api, "is_loopback", return_value=False), patch.object(
            self.admin, "is_loopback", return_value=False
        ), patch.dict(os.environ, {"GATEWAY_API_KEY": "expected-key"}):
            for label, helper in self._auth_helpers():
                with self.subTest(surface=label):
                    error = self.GatewayError if label == "story" else self.AdminError
                    with self.assertRaises(error):
                        helper("Bearer wrong-key")

    def test_keyless_loopback_keeps_bearer_only_development_behavior(self) -> None:
        with patch.object(self.api, "is_loopback", return_value=True), patch.object(
            self.admin, "is_loopback", return_value=True
        ), patch.dict(os.environ, {"GATEWAY_API_KEY": ""}):
            for label, helper in self._auth_helpers():
                with self.subTest(surface=label):
                    helper("Bearer arbitrary")
                    # Admin is intentionally open from loopback, including with
                    # no header. The Story surface still requires a bearer token
                    # even though any token is accepted when keyless.
                    if label == "story":
                        with self.assertRaises(self.GatewayError):
                            helper(None)
                    else:
                        helper(None)

    def _definition(self, provider_id: str, base_url: str):
        return self.ProviderDefinition(
            id=provider_id,
            display_name=provider_id,
            category="language",
            kind=self.KIND_OPENAI_COMPATIBLE,
            base_url=base_url,
            modalities=("chat",),
            discovery=self.DISCOVERY_STATIC,
        )

    def _definition_with_base_override(self, provider_id: str, base_url: str):
        definition = self._definition(provider_id, base_url)
        return self.ProviderDefinition(
            id=definition.id,
            display_name=definition.display_name,
            category=definition.category,
            kind=definition.kind,
            base_url=definition.base_url,
            modalities=definition.modalities,
            discovery=definition.discovery,
            credential_fields=(
                self.CredentialField(
                    key="base_url", label="Base URL", env_var="TEST_BASE_URL",
                    required=False, secret=False,
                ),
            ),
        )

    @staticmethod
    def _chat_row(model_id: str) -> dict[str, object]:
        return {"model_id": model_id, "modality": "chat", "raw": {}}

    def test_batch_models_are_withheld_only_for_openrouter_base_urls(self) -> None:
        inventory = self.ModelInventory(
            self.CredentialStore(path=self._tmp_path / "credentials.json"),
            path=self._tmp_path / "models-cache.json",
        )
        openrouter_proxy = self._definition(
            "custom-openrouter-proxy", "https://openrouter.ai/api/v1"
        )
        unrelated_provider = self._definition(
            "unrelated-openai-compatible", "https://provider.example/v1"
        )

        self.assertFalse(
            inventory.can_publish(
                openrouter_proxy, self._chat_row("openai/gpt-4o:batch")
            )
        )
        self.assertFalse(
            inventory.can_publish(
                self.PROVIDERS_BY_ID["openrouter-main"],
                self._chat_row("openai/gpt-4o:batch"),
            )
        )
        self.assertTrue(
            inventory.can_publish(
                openrouter_proxy, self._chat_row("openai/gpt-4o")
            )
        )
        self.assertTrue(
            inventory.can_publish(
                unrelated_provider, self._chat_row("other/model:batch")
            )
        )

        override_cases = (
            (
                "unrelated overridden to OpenRouter",
                self._definition_with_base_override(
                    "unrelated-with-override", "https://provider.example/v1"
                ),
                "https://openrouter.ai/api/v1",
                False,
            ),
            (
                "OpenRouter overridden to unrelated host",
                self._definition_with_base_override(
                    "openrouter-with-override", "https://openrouter.ai/api/v1"
                ),
                "https://provider.example/v1",
                True,
            ),
        )
        for label, definition, override, batch_allowed in override_cases:
            with self.subTest(override=label):
                store = self.CredentialStore(
                    path=self._tmp_path / f"{definition.id}-credentials.json",
                    definition_lookup=lambda provider_id, definition=definition:
                        definition if provider_id == definition.id else None,
                )
                store.set_values(definition.id, {"base_url": override})
                overridden = self.ModelInventory(
                    store, path=self._tmp_path / f"{definition.id}-models.json"
                )
                self.assertEqual(
                    overridden.can_publish(
                        definition, self._chat_row("provider/model:batch")
                    ),
                    batch_allowed,
                )
                self.assertTrue(
                    overridden.can_publish(
                        definition, self._chat_row("provider/model")
                    )
                )
        self.assertFalse(
            inventory.can_publish(
                self._definition("invalid-url", "https://[bad"),
                self._chat_row("other/model:batch"),
            )
        )


class CatalogueMergeTests(unittest.TestCase):
    """One model offered by several locations is one row that answers for all of them.

    A location is a provider internally but its models publish under the maker's
    namespace, so `Catalog.rebuild` merges the copies. `locked`/`lock_reason`
    merged; `availability` — the same fact in the capability catalogue's own
    vocabulary — kept the **first** location's answer. Measured live on
    2026-09-24: this Mac's Studio Hub was down while both controllers were up, and
    two `studio-hub` TTS rows published `locked: false` beside
    `availability: "unavailable"`, because `studio-hub-local` is iterated first.
    A reader that branches on one and prints the other gets whichever site came
    first.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="gateway-catalogue-merge-")
        tmp = Path(cls._tmp.name)
        cls._env = patch.dict(
            os.environ,
            {
                "GATEWAY_DATA": str(tmp / "data"),
                "GATEWAY_ARTIFACTS": str(tmp / "data" / "artifacts"),
                "GATEWAY_DB": str(tmp / "data" / "jobs.sqlite3"),
                "GATEWAY_CREDENTIALS": str(tmp / "data" / "credentials.json"),
                "GATEWAY_MODEL_CACHE": str(tmp / "data" / "models_cache.json"),
                "GATEWAY_APP_SUPPORT": str(tmp / "app-support"),
                "GATEWAY_ENV_FILE": str(tmp / ".env"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(tmp / "hub-ready.json"),
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_API_KEY": "",
            },
        )
        cls._env.start()
        sys.path.insert(0, str(ROOT))
        from gateway import catalog, fleet
        from gateway.providers import registry

        cls.catalog = catalog
        cls.fleet = fleet
        cls.registry = registry

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.stop()
        cls._tmp.cleanup()

    def _rebuild_with(self, proposals: list[tuple[str, dict]], answering: set[str]):
        """One rebuild over the given (location provider id, model) pairs."""
        definitions = [
            # A location provider: the prefix is what `is_location_provider` reads,
            # and only `studio-hub-local`/`studio-hub-<controller>` carry it.
            SimpleNamespace(
                id=provider_id, kind="studio_hub", display_name="Studio Hub",
                description="Your own Apple Silicon runners.",
            )
            for provider_id, _model in proposals
        ]

        def enabled(modality: str):
            if modality != "image":
                return []
            return [
                (definition, dict(model))
                for definition, (_pid, model) in zip(definitions, proposals)
            ]

        with patch.object(self.registry.models, "enabled_models", enabled), \
                patch.object(self.registry.models, "cached", lambda provider_id: {}), \
                patch.object(self.fleet, "usable_location_ids", lambda: set(answering)), \
                patch.object(self.registry, "definitions", lambda: definitions):
            rebuilt = self.catalog.Catalog()
        rows = [row for row in rebuilt.rows("image") if row["id"].endswith("::hub/model")]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_one_answering_location_offers_the_model_in_both_vocabularies(self) -> None:
        proposals = [
            ("studio-hub-local", {"model_id": "hub/model", "modality": "image",
                                  "display_name": "Hub Model", "raw": {}}),
            ("studio-hub-site-a", {"model_id": "hub/model", "modality": "image",
                                   "display_name": "Hub Model", "raw": {}}),
        ]
        # Only the controller answers; this Mac's Hub is down.
        row = self._rebuild_with(proposals, answering={"site-a"})
        self.assertFalse(row["locked"], row)
        self.assertIsNone(row["lock_reason"])
        self.assertEqual(row["availability"], "available")
        # `available` is the same fact in the vocabulary Story Studio's parsers gate on
        # (`available === true`), so the merge carries it too — and the first site's
        # outage sentence must not survive in the blocker list either (A6/H4).
        self.assertIs(row["available"], True, row)
        self.assertEqual(row["activation_blockers"], [])

    def test_no_answering_location_locks_it_in_both_vocabularies(self) -> None:
        proposals = [
            ("studio-hub-local", {"model_id": "hub/model", "modality": "image",
                                  "display_name": "Hub Model", "raw": {}}),
            ("studio-hub-site-a", {"model_id": "hub/model", "modality": "image",
                                   "display_name": "Hub Model", "raw": {}}),
        ]
        row = self._rebuild_with(proposals, answering=set())
        self.assertTrue(row["locked"], row)
        self.assertTrue(row["lock_reason"])
        self.assertEqual(row["availability"], "unavailable")
        # A dead fleet published `available: true` — the one field Story Studio's model
        # picker actually gates on — so the outage rendered as an offerable model.
        self.assertIs(row["available"], False, row)
        # And the reason is the fleet's own sentence about the outage, never the
        # account/billing code Story Studio renders as a paid-account restriction.
        self.assertIn("No Mac offering this model is reachable", row["lock_reason"])
        self.assertNotIn("account", row["lock_reason"].lower())
        self.assertIn(row["lock_reason"], row["activation_blockers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
