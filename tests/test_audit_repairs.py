"""Fault cases from the 2026-10-07 audit; only isolated fake upstreams and storage."""
from __future__ import annotations

import atexit
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._isolation import sandbox_env
_SANDBOX = sandbox_env('gateway-audit-repairs-')
_SANDBOX._finalizer.detach()

def preserve():
    target = ROOT / '_trash' / 'tests' / ('audit-repairs-' + uuid.uuid4().hex)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(_SANDBOX.name, target)
atexit.register(preserve)

from gateway import api, config, fleet, pools, pool_cooldowns, story_releases, worker
from gateway.adapters.base import BackendError, BackendRequest, BackendResult, note_request_started
from gateway.adapters.pool import PoolBackend
from gateway.providers.definitions import ProviderDefinition
from gateway.store import JobStore


class AuditRepairs(unittest.TestCase):
    def setUp(self):
        self.root = Path(_SANDBOX.name) / uuid.uuid4().hex
        self.root.mkdir()
        self.store = JobStore(self.root / 'jobs.sqlite3')
        self.addCleanup(self.store.close)
        pool_cooldowns.reset()
        self.addCleanup(pool_cooldowns.reset)
        p = patch.object(config, 'DATA_DIR', self.root)
        p.start(); self.addCleanup(p.stop)

    def pool(self, *, free=False, refusal=False, generic=False, accepted=False, settled=False):
        calls = []
        definitions = {p: ProviderDefinition(id=p, display_name=p, category='language',
            kind='openai_compatible', base_url='https://invalid.test', modalities=('chat',),
            discovery='static', free_tier=free) for p in ('a', 'b')}
        class Fake:
            def __init__(self, provider): self.provider = provider
            def generate(self, request):
                calls.append(self.provider)
                note_request_started()
                if self.provider == 'a':
                    if generic: raise RuntimeError('sent; adapter crashed')
                    raise BackendError('sent; response timed out', code='TIMEOUT', status=400 if accepted else None,
                        upstream_accepted=True if accepted else None, upstream_settled=settled,
                        recovery={'request_id': 'responsible-a'})
                if refusal: raise BackendError('provider refusal', code='REFUSED', status=400,
                    upstream_accepted=False)
                return BackendResult(text='from b')
        backend = PoolBackend()
        backend.attach(lambda _, member: (definitions[member[0]], {'model_id': 'x'}),
            lambda d: Fake(d.id))
        backend._credentials_for = lambda _: {}
        return backend, calls

    def test_paid_unknown_send_stops_before_second_member(self):
        for generic, accepted in ((False, False), (True, False), (False, True)):
            with self.subTest(generic=generic, accepted=accepted), patch.object(pools, 'members', return_value=['a::x', 'b::x']):
                backend, calls = self.pool(generic=generic, accepted=accepted)
                with self.assertRaises(BackendError) as caught:
                    backend.generate(BackendRequest(provider_id='pool', model_id='audit', modality='chat',
                        params={}, values={}, base_url=''))
                self.assertEqual(calls, ['a'])
                self.assertEqual(caught.exception.code, 'PROVIDER_SUBMISSION_UNCERTAIN')
                self.assertEqual(caught.exception.recovery['pool_member'], 'a::x')

    def test_free_unknown_send_can_fallback(self):
        with patch.object(pools, 'members', return_value=['a::x', 'b::x']):
            backend, calls = self.pool(free=True)
            self.assertEqual(backend.generate(BackendRequest(provider_id='pool', model_id='audit',
                modality='chat', params={}, values={}, base_url='')).text, 'from b')
            self.assertEqual(calls, ['a', 'b'])

    def test_paid_settled_failure_can_fallback(self):
        with patch.object(pools, 'members', return_value=['a::x', 'b::x']):
            backend, calls = self.pool(accepted=True, settled=True)
            self.assertEqual(backend.generate(BackendRequest(provider_id='pool', model_id='audit',
                modality='chat', params={}, values={}, base_url='')).text, 'from b')
            self.assertEqual(calls, ['a', 'b'])

    def test_paid_unknown_evidence_survives_worker_even_a_400(self):
        job, _ = self.store.submit(modality='chat', model_id='audit', provider_id='pool', params={}, idempotency_key='paid')
        self.store.mark_running(job['id']); self.store.begin_dispatch(job['id'])
        backend, calls = self.pool(refusal=True)
        definition = SimpleNamespace(id='pool', kind='pool', base_url='', display_name='Pool')
        with patch.object(pools, 'members', return_value=['a::x', 'b::x']), \
             patch.object(worker.registry, 'definition', return_value=definition), \
             patch.object(worker.registry.credentials, 'is_enabled', return_value=True), \
             patch.object(worker.registry.credentials, 'resolved', return_value={}), \
             patch.object(worker, 'adapter_for', return_value=backend), \
             patch.object(worker, '_next_machine', return_value=None):
            worker._dispatch(job, self.store, 'pool', 'audit', 'chat', {}, '')
        result = self.store.get(job['id'])
        self.assertEqual(result['recovery_state'], 'unknown_outcome')
        self.assertEqual(result['recovery_context']['request_id'], 'responsible-a')
        self.assertEqual(calls, ['a'])

    def test_offline_cooldown_is_not_subtracted_twice(self):
        pid = fleet.provider_id_for('audit-site')
        row = {'id': 'audit-site', 'machines': [
            {'machine': 'cold', 'label': 'cold', 'reachable': False, 'available_gb': 2},
            {'machine': 'healthy', 'label': 'healthy', 'reachable': True, 'available_gb': 8}]}
        with patch.dict(fleet._MEMORY_COOLDOWN, {}, clear=True):
            fleet.note_memory_refusal(pid, 'm', 'INSUFFICIENT_MEMORY: cold needs about 3.2 GB free')
            self.assertEqual(fleet.usable_slots(row, 'm', 2), 1)
            cold = row['machines'].pop(0)
            self.assertEqual(fleet.usable_slots(row, 'm', 2), 1)  # absent Mac is outside this population
            cold['reachable'] = True
            row['machines'].insert(0, cold)
            self.assertEqual(fleet.usable_slots(row, 'm', 2), 1)  # cooldown survives reconnection
            row['machines'] = []
            self.assertEqual(fleet.usable_slots(row, 'm', 2), 1)  # no roster: conservative old behavior

    def hold(self, jid, provider):
        self.store._conn.execute("INSERT INTO jobs (id, modality, model_id, provider_id, state, params, created_at, updated_at, recovery_state, upstream_job_id) VALUES (?, 'image', 'm', ?, 'failed', '{}', 0, 1, 'unknown_outcome', ?)", (jid, provider, jid))
        self.store._conn.commit()

    def test_sweep_rotates_past_fifty_and_cloud_holds(self):
        for i in range(55): self.hold(f'cloud-{i:03}', 'paid')
        for i in range(55): self.hold(f'hub-{i:03}', 'hub')
        definition = SimpleNamespace(id='hub', kind='studio_hub', base_url='https://invalid.test')
        checked = []
        def snapshot(_, __, batch, **kw):
            checked.append(batch)
            return {'known': True, 'state': 'failed'} if batch == 'hub-054' else None
        with patch.object(worker.registry, 'definitions', return_value=[definition]), \
             patch.object(worker.registry, 'definition', side_effect=lambda p: definition if p == 'hub' else SimpleNamespace(kind='paid')), \
             patch.object(worker.registry.credentials, 'resolved', return_value={}), \
             patch.object(worker.studio_hub, 'batch_snapshot', side_effect=snapshot):
            first = worker.reconcile_holds(self.store)
            second = worker.reconcile_holds(self.store)
        self.assertLessEqual(first['checked'], 50); self.assertLessEqual(second['checked'], 50)
        self.assertEqual(set(checked), {f'hub-{i:03}' for i in range(55)})
        self.assertIsNone(self.store.get('hub-054')['recovery_state'])
        self.assertEqual(self.store.get('cloud-000')['recovery_state'], 'unknown_outcome')

    def test_one_recovery_exception_cannot_starve_the_following_hold(self):
        self.hold('hub-000', 'hub')
        self.hold('hub-001', 'hub')
        definition = SimpleNamespace(id='hub', kind='studio_hub', base_url='https://invalid.test')
        def snapshot(_, __, batch, **kw):
            if batch == 'hub-000': raise RuntimeError('bad upstream report')
            return {'known': True, 'state': 'failed'}
        with patch.object(worker.registry, 'definitions', return_value=[definition]), \
             patch.object(worker.registry, 'definition', return_value=definition), \
             patch.object(worker.registry.credentials, 'resolved', return_value={}), \
             patch.object(worker.studio_hub, 'batch_snapshot', side_effect=snapshot):
            try:
                result = worker.reconcile_holds(self.store)
            except RuntimeError:
                result = {}  # assert on the skipped durable row, not on the injected exception
        self.assertIsNone(self.store.get('hub-001')['recovery_state'])
        self.assertEqual(self.store.get('hub-000')['recovery_state'], 'unknown_outcome')
        self.assertEqual(result['still_open'], 1)

    def test_account_pages_are_distinct_and_count_all_jobs(self):
        for i in range(52): self.hold(f'job-{i:03}', 'paid')
        with patch.object(api, 'store', self.store):
            one = api.account_jobs(page=1, page_size=20)
            two = api.account_jobs(page=2, page_size=20)
            last = api.account_jobs(page=3, page_size=20)
            huge = api.account_jobs(page=10**100, page_size=20)
        self.assertEqual(one['total'], 52)
        self.assertEqual([len(p['items']) for p in (one, two, last, huge)], [20,20,12,0])
        self.assertTrue({p['id'] for p in one['items']}.isdisjoint({p['id'] for p in two['items']}))

    def test_manual_request_is_acknowledged_only_by_its_actual_attempt(self):
        release = {'version': '1.4.258'}
        with patch.object(story_releases, 'current', return_value=release), \
             patch.object(story_releases, 'offer', return_value={}):
            for phase in ('rolled_back', 'failed'):
                with self.subTest(phase=phase):
                    report = {'instance_id': phase, 'version': '1.4.257', 'update':
                        {'phase': phase, 'version': '1.4.258', 'error': 'health check', 'request_id': 'old'}}
                    story_releases.checkin(report)
                    pending = story_releases.request_update(phase).get('update_request_id')
                    self.assertIsNotNone(pending, 'Update Now must name the requested attempt')
                    for old_id in ('old', None, 'different', None):
                        # The old/lost response cannot acknowledge a new attempt, even after restart.
                        old = {**report, 'update': {**report['update'], 'request_id': old_id}}
                        result = story_releases.checkin(old)
                        self.assertTrue(result['update_now'])
                        self.assertEqual(result['update_request_id'], pending)
                        stored = story_releases._read_json(story_releases._instances_file())[phase]
                        self.assertEqual(stored['update_request_id'], pending)
                    failed = {**report, 'update': {**report['update'], 'request_id': pending}}
                    self.assertFalse(story_releases.checkin(failed)['update_now'])
                    self.assertFalse(story_releases.checkin(failed)['update_now'])
                    second = story_releases.request_update(phase)['update_request_id']
                    self.assertNotEqual(second, pending)
                    self.assertTrue(story_releases.checkin(failed)['update_now'])
                    self.assertFalse(story_releases.checkin({**failed, 'version': '1.4.258'})['update_now'])

    def test_legacy_manual_retry_preserves_first_old_report_and_stops_next_failure(self):
        with patch.object(story_releases, 'current', return_value={'version': '1.4.258'}), \
             patch.object(story_releases, 'offer', return_value={}):
            report = {'instance_id': 'legacy', 'version': '1.4.257', 'update':
                {'phase': 'rolled_back', 'version': '1.4.258', 'error': 'health check'}}
            story_releases.checkin(report)
            story_releases.request_update('legacy')
            self.assertTrue(story_releases.checkin(report)['update_now'])
            self.assertFalse(story_releases.checkin(report)['update_now'])

    def test_malformed_startup_phase_fails_closed(self):
        for phase in ([], {}, 1, True):
            with self.subTest(phase=phase):
                fact = fleet._startup_fact({'startup': {'last_update_error': {'phase': phase, 'reason': 'wire report'}}}, {})
                self.assertIsNone(fact['last_update_error']['phase'])

if __name__ == '__main__': unittest.main()
