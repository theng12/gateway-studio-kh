#!/usr/bin/env python3
"""Hub image mode statements are authoritative per worker, including legacy 0.6.215."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env
_SANDBOX = sandbox_env('gateway-hub-image-modes-')

from gateway.providers.definitions import hub_image_modes, image_reference_state, image_reference_limits
from gateway import fleet
from gateway.providers import discovery


def modes(reference_available=True):
    return [
        {'id': 'txt2img', 'label': 'Text to image', 'available': True, 'reason': None},
        {'id': 'edit', 'label': 'Reference to image', 'available': reference_available,
         'reason': None if reference_available else 'This Mac needs more available memory.',
         'references': {'field': 'images', 'min': 1, 'max': 2, 'max_dimension': 1024,
                        'media_types': ['image/png'], 'evidence': {'source': 'worker'}}},
    ]


class HubImageModeTests(unittest.TestCase):
    def test_new_modes_override_stale_legacy_capabilities(self):
        raw = {'image_modes': modes(False), 'capabilities': ['txt2img', 'edit']}
        self.assertFalse(image_reference_state('studio_hub', 'any-name', raw))
        reference = hub_image_modes(raw)[1]
        self.assertFalse(reference['available'])
        self.assertIn('memory', reference['reason'])
        self.assertFalse(fleet.advertises({'raw': raw}, 'edit'))

    def test_new_reference_bounds_and_evidence_are_preserved(self):
        raw = {'image_modes': modes()}
        self.assertEqual(hub_image_modes(raw)[1]['references'], modes()[1]['references'])
        self.assertEqual(image_reference_limits(raw), {'min': 1, 'max': 2, 'max_dimension': 1024})

    def test_text_only_model_cannot_gain_reference_support_from_name(self):
        raw = {'image_modes': modes()[:1], 'capabilities': ['txt2img', 'edit']}
        self.assertFalse(image_reference_state('studio_hub', 'Qwen-edit', raw))
        self.assertEqual([m['id'] for m in hub_image_modes(raw)], ['txt2img'])

    def test_owner_labels_replace_internal_edit_label(self):
        raw = {'image_modes': [{'id': 'edit', 'label': 'Edit', 'available': True}]}
        self.assertEqual(hub_image_modes(raw)[0]['label'], 'Reference to image')

    def test_malformed_or_empty_explicit_modes_fail_closed(self):
        for value in ([], {}, None, [{'id': 'unknown', 'available': True}]):
            raw = {'image_modes': value, 'capabilities': ['txt2img', 'edit']}
            self.assertEqual(hub_image_modes(raw), [])
            self.assertFalse(fleet.advertises({'raw': raw}, 'edit'))
            self.assertFalse(fleet.advertises({'raw': raw}, 'txt2img'))

    def test_legacy_reference_lane_false_overrides_stale_token(self):
        raw = {'capabilities': ['txt2img', 'edit'], 'reference_lane': {'available': False, 'reason': 'Not ready'}}
        self.assertFalse(image_reference_state('studio_hub', 'fixture', raw))

    def test_legacy_215_tokens_still_work_without_new_field(self):
        raw = {'capabilities': ['txt2img', 'edit'], 'lane_contract': {'references': {'min': 1, 'max': 2}}}
        self.assertEqual([m['label'] for m in hub_image_modes(raw)], ['Text to image', 'Reference to image'])
        self.assertTrue(image_reference_state('studio_hub', 'fixture', raw))
        self.assertTrue(fleet.advertises({'raw': raw}, 'edit'))

    def test_qualified_worker_is_not_hidden_by_a_text_ready_controller(self):
        raw = {'image_workers': [
            {'hub_machine': 'local', 'hub_cached': True, 'image_modes': modes(False)},
            {'hub_machine': 'node', 'hub_cached': True, 'image_modes': modes(True)},
        ]}
        reference = hub_image_modes(raw)[1]
        self.assertTrue(reference['available'])
        self.assertEqual([(w['machine_id'], w['available']) for w in reference['workers']], [('local', False), ('node', True)])

    def test_an_unready_worker_cannot_make_a_site_reference_capable(self):
        raw = {'image_workers': [{'hub_machine': 'node', 'hub_cached': False, 'image_modes': modes()}]}
        self.assertFalse(fleet.advertises({'raw': raw}, 'edit'))

    def test_available_workers_share_conservative_reference_bounds(self):
        first, second = modes(), modes()
        second[1]['references'] = {'min': 2, 'max': 4, 'max_dimension': 2048}
        raw = {'image_workers': [{'image_modes': first}, {'image_modes': second}]}
        self.assertEqual(image_reference_limits(raw), {'min': 2, 'max': 2, 'max_dimension': 1024})

    def test_request_capability_is_derived_from_reference_intent(self):
        self.assertEqual(fleet.required_capability('image', {}), 'txt2img')
        self.assertEqual(fleet.required_capability('image', {'_reference_digests': []}), 'edit')
        self.assertIsNone(fleet.required_capability('chat', {}))

    def test_discovery_retains_all_worker_mode_statements(self):
        rows = [
            {'repo': 'fixture', 'hub_machine': 'local', 'hub_cached': True, 'image_modes': modes(False)},
            {'repo': 'fixture', 'hub_machine': 'node', 'hub_cached': True, 'image_modes': modes(True)},
        ]
        response = SimpleNamespace(status_code=200, json=lambda: {'models': rows})
        with patch.object(discovery.httpx, 'get', return_value=response):
            indexed = discovery._hub_catalog('http://fixture', 'fixture-token')
        self.assertEqual(len(indexed['fixture']['image_workers']), 2)
        self.assertTrue(hub_image_modes(indexed['fixture'])[1]['available'])

    def test_catalogue_publishes_modes_and_merges_qualified_sites(self):
        from gateway import catalog
        definition = SimpleNamespace(id='studio-hub-one', kind='studio_hub')
        first = catalog._image_row('fixture', definition, {'raw': {'image_workers': [
            {'hub_machine': 'local', 'image_modes': modes(False)},
        ]}})
        self.assertFalse(first['image_modes'][1]['available'])
        self.assertEqual(first['image_modes'][1]['workers'][0]['provider_id'], 'studio-hub-one')
        second = catalog._image_row('fixture', SimpleNamespace(id='studio-hub-two', kind='studio_hub'),
                                    {'raw': {'image_workers': [{'hub_machine': 'local', 'image_modes': modes()}]}})
        catalog._merge_capabilities(first, second, 'image')
        reference = first['image_modes'][1]
        self.assertTrue(reference['available'])
        self.assertEqual({w['provider_id'] for w in reference['workers']}, {'studio-hub-one', 'studio-hub-two'})
        self.assertEqual(reference['references']['max'], first['max_reference_images'])
        self.assertEqual(first['operations'], ['text-to-image', 'image-to-image'])

    def test_catalogue_reference_only_mode_does_not_advertise_text(self):
        from gateway import catalog
        row = catalog._image_row('fixture', SimpleNamespace(id='studio-hub-one', kind='studio_hub'),
                                 {'raw': {'image_modes': modes()[1:]}})
        self.assertEqual(row['operations'], ['image-to-image'])
        self.assertEqual([m['label'] for m in row['image_modes']], ['Reference to image'])

    def test_unknown_and_mismatched_request_modes_are_refused(self):
        from gateway import api
        for payload in ({'mode': 'unknown'}, {'mode': 'edit'},
                        {'mode': 'txt2img', 'reference_asset_ids': []}):
            with self.subTest(payload=payload), self.assertRaises(api.GatewayError) as caught:
                api._stage_image_references(payload)
            self.assertEqual(caught.exception.code, 'INVALID_PARAMS')

    def test_pinned_unqualified_site_is_refused_with_its_reason(self):
        from gateway import api
        with self.assertRaises(api.GatewayError) as caught:
            api._refuse_references_this_lane_cannot_take(
                SimpleNamespace(kind='studio_hub'), {'raw': {'image_modes': modes(False)}},
                {'_reference_digests': ['reference']},
            )
        self.assertIn('Reference to image', str(caught.exception))
        self.assertIn('memory', str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
