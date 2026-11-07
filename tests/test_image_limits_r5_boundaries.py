"""Exercise r5 admission through discovery, submission and late routing, offline."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
# The fixture module establishes a disposable profile before importing Gateway modules.
from test_image_limits_r5 import MODEL, request, row, snapshot, worker
from gateway import api, catalog, config, fleet
from gateway.providers import discovery, registry
from gateway.worker import Worker
from test_free_location_reroute import _Case, _Fleet, _Registry, A, B, PNG


def definition(site):
    return SimpleNamespace(id='studio-hub-' + site, display_name=site, kind='studio_hub')


def model(maximum):
    return {'model_id': MODEL, 'raw': {'image_workers': [worker(
        snapshot_value=snapshot(edit_rows=[row(None, maximum=maximum)]))]}}


class BoundaryTests(unittest.TestCase):
    def sites(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(registry.models, 'enabled_models', return_value=[
            (definition('denied'), model(1)), (definition('allowed'), model(4))]))
        stack.enter_context(patch.object(fleet, 'snapshot', return_value={'locations': [
            {'id': site, 'reachable': True,
             'capacity': {'eligible_physical_machine_slots_total': 3}}
            for site in ('denied', 'allowed')]}))
        stack.enter_context(patch.object(fleet, 'is_draining_provider', return_value=False))
        stack.enter_context(patch.object(fleet, 'speed_ranked', return_value=False))

    def test_discovery_snapshot_reaches_published_catalogue(self):
        payload = worker(snapshot_value=snapshot(edit_rows=[row(None, maximum=1)]))
        payload['repo'] = MODEL
        response = SimpleNamespace(status_code=200, json=lambda: {'models': [payload]})
        with patch.object(discovery.httpx, 'get', return_value=response):
            raw = discovery._hub_catalog('http://fixture', 'fixture-token')[MODEL]
        published = catalog._image_row(MODEL, definition('site'), {'raw': raw})
        self.assertEqual(published['max_reference_images'], 1)
        self.assertEqual(published['image_modes'][1]['references']['max'], 1)

    def test_submit_refuses_selected_snapshot_before_store(self):
        # Resolve an explicit lane so the second submit guard must inspect that lane's snapshot.
        with patch.object(api.catalog, 'resolve', return_value=(definition('denied'), model(1))), \
                patch.object(registry.models, 'enabled_models', return_value=[]), \
                patch.object(api, '_refuse_when_the_queue_is_full'), \
                patch.object(api, '_refuse_when_the_disk_is_low'), \
                patch.object(api.store, 'submit') as submit:
            with self.assertRaises(api.GatewayError) as caught:
                api._submit('image', 'studio-hub-denied::' + MODEL, request(2), '')
        self.assertEqual(caught.exception.code, 'IMAGE_LIMITS_NO_NODE')
        submit.assert_not_called()

    def test_router_uses_snapshot_instead_of_legacy_permission(self):
        self.sites()
        candidates = fleet.routing_candidates(MODEL, 'image', image_params=request(2))
        self.assertEqual([value['location_id'] for value in candidates], ['allowed'])
        self.assertEqual(candidates[0]['slots'], 1)
        self.assertEqual(candidates[0]['image_limit_machines'], ['node-a'])

    def test_rebind_routes_exact_count_and_keeps_measurement_pin(self):
        self.sites()
        instance = Worker.__new__(Worker)
        instance.store = SimpleNamespace(rebind_unsent=Mock(return_value=True),
                                         attempted_locations=lambda _: [])
        instance._nowhere = set()
        instance._rebind_backoff = {}
        instance._job_room = lambda *_: 0
        instance._site_room = lambda *_: (1, 0, 0)
        job = {'id': 'fixture-r5', 'provider_id': 'studio-hub-original',
               'model_id': MODEL, 'modality': 'image', 'state': 'accepted',
               'params': json.dumps(request(2))}
        self.assertTrue(instance._rebind(job))
        self.assertEqual(job['provider_id'], 'studio-hub-allowed')
        self.assertEqual(instance.store.rebind_unsent.call_args.kwargs['to_provider'],
                         'studio-hub-allowed')
        instance.store.rebind_unsent.reset_mock()
        job['params'] = json.dumps({**request(2), 'measurement_target': {
            'location_id': 'denied', 'machine': 'node-a'}})
        self.assertFalse(instance._rebind(job))
        instance.store.rebind_unsent.assert_not_called()


class DispatchBoundaryTests(_Case):
    def test_changed_snapshot_refuses_before_send_and_routes_to_allowed_site(self):
        class ReferenceFleet(_Fleet):
            def handler(self, request):
                response = super().handler(request)
                if request.method == 'GET' and request.url.path == '/api/hub/jobs/batch-b':
                    answer = response.json()
                    answer['items'][0]['terminal_result']['reference_count'] = 2
                    return httpx.Response(200, json=answer)
                return response

        transport = ReferenceFleet(a_dies=False)
        registry_fixture = _Registry()
        models = []
        for provider, maximum in ((A, 1), (B, 4)):
            definition_fixture = registry_fixture.definition(provider)
            raw = {'image_workers': [worker(snapshot_value=snapshot(
                model='model/test', edit_rows=[row(None, maximum=maximum)]))]}
            models.append((definition_fixture, {'model_id': 'model/test', 'raw': raw}))
        registry_fixture.models.enabled_models = lambda _: models
        reference = Path(config.ARTIFACT_DIR) / 'r5-reference.png'
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(PNG)
        params = {**request(2), '_reference_paths': [str(reference), str(reference)],
                  '_reference_types': ['image/png', 'image/png']}
        job = self.job('r5-dispatch-change', params)
        with patch.object(registry.models, 'enabled_models', return_value=models):
            finished = self.run_job(transport, job, registry_fixture)
        self.assertEqual(finished['state'], 'succeeded', finished.get('error_detail'))
        self.assertEqual(finished['provider_id'], B)
        self.assertEqual(transport.submits('a'), [])
        self.assertEqual(len(transport.submits('b')), 1)


if __name__ == '__main__':
    unittest.main()
