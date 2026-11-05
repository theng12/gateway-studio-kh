"""No-network image table, exact-count admission and durable intent checks."""
from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from _isolation import sandbox_env
_SANDBOX = sandbox_env('gateway-image-limits-')
from gateway import catalog, fleet, image_limits
from gateway.providers import registry

MODEL = 'maker/fixture-4bit'
DEFINITION = SimpleNamespace(id='studio-hub-site', display_name='Fixture site', kind='studio_hub')


def worker(machine='node', maximum=1, tier='24'):
    tiers = {tier: {'allowed': True, 'max_reference_images': maximum, 'swap_allowance_gb': 0,
                    'source': 'provisional, measuring', 'receipt': 'fixture only'}}
    table = {'schema': image_limits.SCHEMA, 'revision': 'fixture-one',
             'swap_allowance_units': 'decimal GB of host swap growth from job baseline',
             'rows': [{'model_id': MODEL, 'mode': mode, 'reference_count': None, 'tiers': copy.deepcopy(tiers)}
                      for mode in ['txt2img', 'edit']]}
    return {'hub_machine': machine, 'hub_cached': True, 'runtime_ready': True, 'memory_eligible': True,
            'lane_contract': {'reference_limits_table': table, 'references': {'tier': tier}},
            'image_modes': [{'id': 'txt2img', 'available': True},
                            {'id': 'edit', 'available': True, 'references': {'min': 1, 'max': maximum, 'tier': tier}}]}


def request(count):
    return {'_reference_digests': list(range(count))}


class ReaderTests(unittest.TestCase):
    def test_exact_count_selects_only_fitting_workers(self):
        raw = {'image_workers': [worker('one', 1), worker('two', 2)]}
        self.assertEqual(image_limits.assess(raw, MODEL, request(1))['eligible'], 2)
        self.assertEqual(image_limits.assess(raw, MODEL, request(2))['machines'], ['two'])
        refused = image_limits.assess(raw, MODEL, request(3))
        self.assertFalse(refused['allowed'])
        self.assertIn('3 reference', refused['reason'])
        self.assertIn(MODEL, refused['reason'])

    def test_explicit_count_off_overrides_general_permission(self):
        raw = worker(maximum=3)
        value = raw['lane_contract']['reference_limits_table']
        row = copy.deepcopy(value['rows'][1])
        row.update(reference_count=2)
        row['tiers']['24']['allowed'] = False
        value['rows'].append(row)
        self.assertFalse(image_limits.assess(raw, MODEL, request(2))['allowed'])
        self.assertTrue(image_limits.assess(raw, MODEL, request(3))['allowed'])
        self.assertEqual(image_limits.published(raw, MODEL)['max'], 1)

    def test_target_cannot_borrow_another_nodes_allowance(self):
        raw = {'image_workers': [worker('one', 1), worker('two', 2)]}
        params = {**request(2), 'measurement_target': {'machine': 'one'}}
        self.assertFalse(image_limits.assess(raw, MODEL, params)['allowed'])

    def test_zero_and_false_are_not_unknown(self):
        raw = worker(maximum=0)
        raw['lane_contract']['reference_limits_table']['rows'][1]['tiers']['24']['allowed'] = False
        self.assertFalse(image_limits.assess(raw, MODEL, request(1))['allowed'])
        self.assertTrue(image_limits.assess(raw, MODEL, {})['allowed'])
        self.assertEqual(image_limits.published(raw, MODEL), {'txt2img': True, 'edit': False, 'max': 0})

    def test_missing_unknown_and_ambiguous_cells_fail_closed(self):
        for mutation in ['tier', 'revision', 'schema', 'source', 'duplicate', 'swap', 'boolean_max']:
            raw = worker()
            table = raw['lane_contract']['reference_limits_table']
            if mutation == 'tier': raw['lane_contract']['references']['tier'] = 'unknown'
            elif mutation == 'duplicate': table['rows'].append(copy.deepcopy(table['rows'][1]))
            elif mutation == 'swap': table['rows'][1]['tiers']['24']['swap_allowance_gb'] = float('nan')
            elif mutation == 'boolean_max': table['rows'][1]['tiers']['24']['max_reference_images'] = True
            elif mutation == 'source': table['rows'][1]['tiers']['24']['source'] = ''
            else: table[mutation] = None
            with self.subTest(mutation=mutation):
                self.assertFalse(image_limits.assess(raw, MODEL, request(1))['allowed'])

    def test_legacy_rows_keep_legacy_behavior(self):
        self.assertIsNone(image_limits.assess({'image_modes': [{'id': 'txt2img', 'available': True}]}, MODEL, {}))

    def test_physical_tier_conversion_uses_binary_gib_and_live_bands(self):
        self.assertEqual(image_limits.worker_tier({'memory_gb': 34.359738368}, {'8': {}, '24': {}, '32': {}}), '32')
        self.assertIsNone(image_limits.worker_tier({'memory_gb': None}, {'24': {}}))

    def test_unready_workers_do_not_increase_capacity(self):
        raw = worker()
        raw['runtime_ready'] = False
        self.assertEqual(image_limits.assess(raw, MODEL, request(1))['eligible'], 0)

    def test_catalogue_and_admission_use_same_confirmed_table(self):
        raw = worker(maximum=2)
        row = catalog._image_row(MODEL, DEFINITION, {'raw': raw})
        self.assertEqual(row['max_reference_images'], 2)
        self.assertTrue(image_limits.assess(raw, MODEL, request(row['max_reference_images']))['allowed'])
        self.assertFalse(image_limits.assess(raw, MODEL, request(3))['allowed'])

    def test_table_minimum_is_identical_in_flat_and_nested_catalogue_fields(self):
        raw = worker(maximum=2)
        raw['image_modes'][1]['references']['min'] = 2
        row = catalog._image_row(MODEL, DEFINITION, {'raw': raw})
        edit = next(mode for mode in row['image_modes'] if mode['id'] == 'edit')
        self.assertEqual(edit['references']['min'], row['min_reference_images'])
        self.assertTrue(image_limits.assess(raw, MODEL, request(row['min_reference_images']))['allowed'])

    def test_legacy_reference_reader_preserves_zero_and_rejects_boolean_or_negative(self):
        from gateway.providers.definitions import image_reference_limits
        for field in ['min', 'max', 'max_dimension']:
            for value, expected in [(0, 0), (None, None), (False, None), (-1, None), (2, 2)]:
                with self.subTest(field=field, value=value):
                    raw = {'lane_contract': {'references': {field: value}}}
                    self.assertEqual(image_reference_limits(raw)[field], expected)

    def test_legacy_zero_maximum_is_enforced_at_the_submit_door(self):
        from gateway import api
        raw = {'image_modes': [{'id': 'edit', 'available': True, 'references': {'max': 0}}]}
        with self.assertRaises(api.GatewayError) as error:
            api._refuse_references_this_lane_cannot_take(DEFINITION, {'model_id': MODEL, 'raw': raw}, request(1))
        self.assertIn('at most 0', str(error.exception))


class IntentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'intent.json'
        self.raw = worker()
        self.models = [{'model_id': MODEL, 'modality': 'image', 'raw': self.raw}]
        for patcher in [patch.object(image_limits, '_path', return_value=self.path),
                        patch.object(registry, 'definitions', return_value=[DEFINITION]),
                        patch.object(registry.models, 'models_for', return_value=self.models)]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.payload = {'provider_id': DEFINITION.id, 'model_id': MODEL, 'mode': 'edit',
                        'reference_count': None, 'tier': '24',
                        'change': {'allowed': True, 'max_reference_images': 5, 'swap_allowance_gb': 2.5}}

    def cell(self, view):
        return next(row for row in view['rows'] if row['mode'] == 'edit')['tiers']['24']

    def test_saved_intent_is_durable_but_never_becomes_effective(self):
        cell = self.cell(image_limits.save(self.payload))
        self.assertEqual(cell['status'], 'awaiting_hub_support')
        self.assertEqual(cell['change']['max_reference_images'], 5)
        self.assertEqual(cell['effective']['max_reference_images'], 1)
        self.assertEqual(cell['effective']['source'], 'provisional, measuring')
        self.assertEqual(self.cell(image_limits.view()), cell)
        self.assertFalse(image_limits.assess(self.raw, MODEL, request(2))['allowed'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_reset_reads_current_default_after_default_changes(self):
        image_limits.save(self.payload)
        self.raw['lane_contract']['reference_limits_table']['rows'][1]['tiers']['24']['max_reference_images'] = 2
        changed = self.cell(image_limits.view())
        self.assertEqual(changed['default']['max_reference_images'], 2)
        reset = self.cell(image_limits.save({**self.payload, 'change': None}))
        self.assertIsNone(reset['change'])
        self.assertEqual(reset['effective']['max_reference_images'], 2)
        self.assertEqual(reset['status'], 'hub_default')

    def test_invalid_or_unpublished_intent_does_not_write(self):
        invalid = [dict(self.payload, tier='unknown'), dict(self.payload, tier=[]),
                   dict(self.payload, reference_count=True), dict(self.payload, extra=True)]
        for value in [-1, True, float('nan'), float('inf')]:
            invalid.append({**self.payload, 'change': {**self.payload['change'], 'swap_allowance_gb': value}})
        invalid.append({**self.payload, 'change': {**self.payload['change'], 'max_reference_images': True}})
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                image_limits.save(payload)
            self.assertFalse(self.path.exists())

    def test_owner_routes_refuse_a_remote_bare_key(self):
        from fastapi.testclient import TestClient
        from gateway import api
        client = TestClient(api.app, client=('remote.invalid', 12345))
        for method in ['GET', 'PUT']:
            response = client.request(method, '/admin/api/settings/image-limits',
                                      headers={'Authorization': 'Bearer fixture-key'},
                                      **({'json': self.payload} if method == 'PUT' else {}))
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.path.exists())

    def test_corrupt_store_is_a_loud_error(self):
        self.path.write_text('broken')
        with self.assertRaisesRegex(ValueError, 'cannot be read'):
            image_limits.view()


class RoutingTests(unittest.TestCase):
    def test_real_candidates_filter_sites_and_weight_only_allowed_nodes(self):
        locations = [{'id': site, 'reachable': True, 'machines': [],
                      'machine_summary': {'reachable': 3}, 'capacity': {'eligible_physical_machine_slots_total': 3}}
                     for site in ['one', 'two']]
        models = [(SimpleNamespace(id='studio-hub-' + site, kind='studio_hub'),
                   {'model_id': MODEL, 'raw': worker(maximum=maximum)})
                  for site, maximum in [('one', 1), ('two', 2)]]
        with patch.object(fleet, 'snapshot', return_value={'locations': locations}), \
             patch.object(registry.models, 'enabled_models', return_value=models), \
             patch.object(fleet, 'is_draining_provider', return_value=False), \
             patch.object(fleet, 'speed_ranked', return_value=False):
            both = fleet.routing_candidates(MODEL, 'image', image_params=request(1))
            self.assertEqual({row['location_id'] for row in both}, {'one', 'two'})
            two = fleet.routing_candidates(MODEL, 'image', image_params=request(2))
            self.assertEqual([row['location_id'] for row in two], ['two'])
            self.assertEqual(two[0]['slots'], 1)

    def test_pool_cannot_borrow_unassigned_nodes_permission(self):
        candidate = {'location_id': 'site', 'slots': 2, 'image_limit_machines': ['allowed']}
        with patch.object(fleet, 'snapshot', return_value={'locations': [{'id': 'site'}]}), \
             patch.object(fleet, 'routing_candidates', return_value=[candidate]), \
             patch.object(fleet, 'assigned_macs', return_value={'different'}):
            self.assertEqual(fleet.pool_candidates([MODEL], 'image', image_params=request(2)), [])

    def test_paused_or_draining_site_cannot_supply_permission_for_active_sites(self):
        models = [(SimpleNamespace(id='studio-hub-' + site, kind='studio_hub'),
                   {'model_id': MODEL, 'raw': worker(maximum=maximum)})
                  for site, maximum in [('active', 1), ('unavailable', 2)]]
        for unavailable in ({'paused_at': 1}, {'drained': True},
                            {'capacity': {'accepting_customer_jobs': False}}):
            with self.subTest(unavailable=unavailable), \
                 patch.object(registry.models, 'enabled_models', return_value=models), \
                 patch.object(fleet, 'snapshot', return_value={'locations': [
                     {'id': 'active'}, {'id': 'unavailable', **unavailable}]}), \
                 patch.object(fleet, 'is_draining_provider', return_value=False):
                self.assertIn('edit, 2 reference',
                              image_limits.refusal('studio-hub::' + MODEL, request(2)) or '')

    def test_all_declared_sites_refuse_with_exact_job_type(self):
        model = {'model_id': MODEL, 'raw': worker(maximum=1)}
        with patch.object(registry.models, 'enabled_models', return_value=[(DEFINITION, model)]):
            reason = image_limits.refusal('studio-hub::' + MODEL, request(2))
            self.assertIn('edit, 2 reference', reason)
            self.assertIsNone(image_limits.refusal('studio-hub::' + MODEL, request(1)))


class AdmissionTests(unittest.TestCase):
    def test_explicit_lane_uses_exact_node_count_and_named_refusal(self):
        from gateway import api
        model = {'model_id': MODEL, 'raw': {'image_workers': [worker('one', 1), worker('two', 2)]}}
        api._refuse_references_this_lane_cannot_take(DEFINITION, model, request(2))
        with self.assertRaises(api.GatewayError) as error:
            api._refuse_references_this_lane_cannot_take(DEFINITION, model, request(3))
        self.assertEqual(error.exception.code, 'IMAGE_LIMITS_NO_NODE')

    def test_queued_denial_settles_but_alternate_location_and_accepted_work_do_not(self):
        from gateway.worker import Worker
        instance = Worker.__new__(Worker)
        instance.store = SimpleNamespace(fail=Mock())
        job = {'id': 'fixture', 'provider_id': DEFINITION.id, 'model_id': MODEL,
               'modality': 'image', 'state': 'accepted', 'params': json.dumps(request(2))}
        decision = image_limits.assess(worker(), MODEL, request(2))
        with patch.object(image_limits, 'for_provider', return_value=decision), \
             patch.object(image_limits, 'refusal', return_value=decision['reason']):
            self.assertTrue(instance._image_limits_refused(job))
            self.assertEqual(instance.store.fail.call_args.kwargs['code'], 'IMAGE_LIMITS_NO_NODE')
            for guard in [{'upstream_job_id': 'accepted-batch'}, {'dispatch_started_at': 1}]:
                self.assertFalse(instance._image_limits_refused({**job, **guard}))
        with patch.object(image_limits, 'for_provider', return_value=decision), \
             patch.object(image_limits, 'refusal', return_value=None):
            self.assertFalse(instance._image_limits_refused(job))

    def test_rebind_negative_cache_keeps_reference_counts_separate(self):
        from gateway import worker as worker_module
        instance = worker_module.Worker.__new__(worker_module.Worker)
        instance.store = SimpleNamespace(rebind_unsent=Mock(return_value=True), attempted_locations=lambda _: [])
        instance._nowhere = set()
        instance._rebind_backoff = {}
        instance._job_room = lambda *_: 0
        instance._site_room = lambda *_: (1, 0, 0)
        job = {'id': 'two', 'provider_id': DEFINITION.id, 'model_id': MODEL,
               'modality': 'image', 'state': 'accepted', 'params': json.dumps(request(2))}
        candidate = {'provider_id': 'studio-hub-other', 'location_id': 'other', 'room': 1}
        with patch.object(fleet, 'routing_candidates', side_effect=[[], [candidate]]) as routes, \
             patch.object(fleet, 'apply_load', side_effect=lambda rows, _: rows), \
             patch.object(fleet, 'speed_ranked', return_value=False), \
             patch.object(fleet, 'rank_candidate', return_value=(0,)):
            self.assertFalse(instance._rebind(job))
            one = {**job, 'id': 'one', 'params': json.dumps(request(1))}
            self.assertTrue(instance._rebind(one))
            self.assertEqual(routes.call_args.kwargs['image_params'], request(1))


if __name__ == '__main__':
    unittest.main()
