"""Provider switches preserve saved decisions and gate the shared offer/route list."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env

_SANDBOX = sandbox_env('gateway-provider-switch-')

from gateway.providers.credentials import CredentialStore
from gateway.providers.definitions import PROVIDERS_BY_ID
from gateway.providers.models import ModelInventory
from gateway.providers.registry import ProviderRegistry


class ProviderSwitchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(_SANDBOX.name) / self._testMethodName
        self.definition = PROVIDERS_BY_ID['openai-direct']
        self.provider = self.definition.id
        # No inherited API key can make this isolated test look configured.
        self.enterContext(patch('gateway.providers.credentials.get', return_value=''))
        self.store = CredentialStore(path=self.root / 'credentials.json')
        self.inventory = ModelInventory(self.store, path=self.root / 'models.json',
                                        definitions_lookup=lambda: [self.definition])
        self.inventory._data['providers'][self.provider] = {
            'models': [{'model_id': 'probe-chat', 'modality': 'chat'}]}
        self.inventory.set_model_enabled(self.provider, 'probe-chat', True)
        self.registry = ProviderRegistry()
        self.registry.credentials = self.store
        self.registry.models = self.inventory

    def test_default_on_without_credentials_does_not_offer_or_route(self):
        self.assertTrue(self.store.is_enabled(self.provider))
        self.assertFalse(self.store.is_configured(self.provider))
        self.assertEqual(self.inventory.enabled_models('chat'), [])
        self.assertEqual(self.registry.resolver('chat', 'probe-chat'), (None, None))

    def test_off_survives_reload_and_key_change_then_on_restores_model(self):
        self.store.set_values(self.provider, {'api_key': 'isolated-key'})
        self.assertTrue(self.store.is_configured(self.provider))
        self.assertEqual(self.registry.resolver('chat', 'probe-chat')[0], self.definition)
        decisions = dict(self.inventory._data['enabled'][self.provider])
        self.store.set_enabled(self.provider, False)
        self.store.set_values(self.provider, {'api_key': 'isolated-replacement'})
        self.store = CredentialStore(path=self.store.path)
        self.inventory.credentials = self.store
        self.assertFalse(self.store.is_enabled(self.provider))
        self.assertTrue(self.store.is_configured(self.provider))
        self.assertEqual(self.inventory.enabled_models('chat'), [])
        self.assertEqual(self.registry.resolver('chat', 'probe-chat'), (None, None))
        self.store.set_enabled(self.provider, True)
        self.assertEqual([m['model_id'] for _, m in self.inventory.enabled_models('chat')], ['probe-chat'])
        self.assertEqual(self.registry.resolver('chat', 'probe-chat')[0], self.definition)
        reloaded = ModelInventory(self.store, path=self.inventory.path)
        self.assertEqual(reloaded._data['enabled'][self.provider], decisions)


if __name__ == '__main__':
    unittest.main()
