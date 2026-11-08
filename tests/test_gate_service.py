"""Real archive/ledger recovery tests with an in-process Hub; no fleet effects."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tarfile
import time
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env
PROFILE=sandbox_env('gate-service-')

from gateway.developer_contract import DeveloperTestError
from gateway.gate_contract import encoded
from gateway.gate_service import GateService
from gateway.gate_transport import GateTransport
from tests.gate_fixtures import (fixture, make_acceptance, make_status, make_result_descriptor,
                                 result_tar, make_source_tar_bytes, now_utc_str)


class FakeHub(GateTransport):
    def __init__(self, request, cap):
        super().__init__()
        self.request=copy.deepcopy(request)
        self.cap=copy.deepcopy(cap)
        self.calls=[]
        self.acceptance=None
        self.current=None
        self.archive=None
        self.lose_submit=False
        self.lose_source=False
        self.lose_ack=False
        self.service=None

    def identity(self, controller): return 'http://fixture.test','local-only-fixture-token','original-pin'

    def json(self, controller,node,operation,**kwargs):
        self.calls.append(operation)
        if kwargs.get('pin') not in (None,'original-pin'):
            raise DeveloperTestError(409,'DESTINATION_CHANGED','Original pin changed.')
        if operation=='capabilities': return encoded(self.cap)
        if operation=='submit':
            self.request=json.loads(kwargs['raw'])
            self.acceptance=make_acceptance(self.request,idempotency_key=kwargs['key'])
            self.current=make_status(self.request,self.acceptance,self.cap,state='waiting_source',reason=None)
            if self.lose_submit: raise DeveloperTestError(502,'TRANSPORT_UNCERTAIN','Submit response lost.')
        if operation=='source':
            self.current=make_status(self.request,self.acceptance,self.cap,state='waiting_idle',reason=None)
            if self.lose_source: raise DeveloperTestError(502,'TRANSPORT_UNCERTAIN','Source response lost.')
        if operation=='ack':
            # ACK cannot precede durable verified archive/ledger, including after reopen.
            rows=self.service.store.rows()
            self.assert_row=rows[0]
            assert self.assert_row['result_verified']
            assert self.service.result_path(self.assert_row['id']).read_bytes()==self.archive
            body=json.loads(kwargs['raw'])
            assert body['acceptance']==self.acceptance
            assert body['result']==self.current['result']
            self.current.update(result_acknowledged=True,acknowledged_result=body['result'],acknowledged_at=now_utc_str())
            # ACK is persisted before cleanup; it is valid for workspace still to exist.
            if self.lose_ack: raise DeveloperTestError(502,'TRANSPORT_UNCERTAIN','ACK response lost.')
        if operation=='cancel':
            self.current['reason']={'code':'CANCEL_REQUESTED','detail':'Release not yet confirmed.'}
        return encoded(self.current)

    def complete(self):
        self.archive=result_tar(self.request,self.cap,self.acceptance)
        self.current=make_status(self.request,self.acceptance,self.cap,result_descriptor=make_result_descriptor(self.archive))

    def download(self,controller,node,key,descriptor,destination,**kwargs):
        self.calls.append('result')
        assert hashlib.sha256(self.archive).hexdigest()==descriptor['sha256']
        GateService._write_new(destination,self.archive)


class GateServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.request,self.cap,self.policy=fixture()
        self.hub=FakeHub(self.request,self.cap)
        self.inventory={'generated_at':time.time(),
            'locations':[{'id':'c1','controller_id':'c1','reachable':True,'enabled':True}],
            'machines':[{'location_id':'c1','machine':'n1','reachable':True,'node_paused':False}]}
        self.service=GateService(self.root,transport=self.hub,inventory=lambda:self.inventory)
        self.hub.service=self.service
        self.service.save_policy(encoded({'expected_revision':'unconfigured','policy':self.policy}))

    def start(self): return self.service.start('c1','n1',encoded(self.request))

    def test_default_policy_admits_nobody_and_shows_reader_caps(self):
        other=GateService(self.root/'new',transport=self.hub,inventory=lambda:self.inventory)
        self.assertEqual(other.view()['policy']['allowed_targets'],[])
        self.assertEqual(other.policy()['minimum_internal_physical_bytes'],500000000000)
        self.assertGreater(other.view()['reader_limits']['local_spool_bytes'],0)
        with self.assertRaises(DeveloperTestError): other.start('c1','n1',encoded(self.request))
        self.assertNotIn('submit',self.hub.calls)

    def test_new_default_does_not_rewrite_an_existing_owner_policy(self):
        policy = copy.deepcopy(self.policy)
        policy.update(revision='owner-higher-minimum', minimum_internal_physical_bytes=512000000000)
        self.service.save_policy(encoded({'expected_revision': self.policy['revision'], 'policy': policy}))
        reopened = GateService(self.root, transport=self.hub, inventory=lambda: self.inventory)
        self.assertEqual(reopened.policy(), policy)

    def test_lost_submit_reopens_and_reconciles_without_resubmit(self):
        self.hub.lose_submit=True
        first=self.start()
        self.assertEqual(first['state'],'recovery_hold')
        reopened=GateService(self.root,transport=self.hub,inventory=lambda:self.inventory)
        self.hub.service=reopened
        self.assertEqual(reopened.start('c1','n1',encoded(self.request))['attempt'],first['attempt'])
        self.assertEqual(reopened.refresh(first['attempt'])['state'],'waiting_source')
        self.assertEqual(self.hub.calls.count('submit'),1)
        self.assertNotIn('source',self.hub.calls)

    def test_source_upload_uncertainty_never_replays_source(self):
        first=self.start()
        path,size=self.service.new_source(first['attempt'])
        archive,_=make_source_tar_bytes()
        self.assertEqual(len(archive),size)
        GateService._write_new(path,archive)
        self.hub.lose_source=True
        self.assertEqual(self.service.upload(first['attempt'],path)['state'],'recovery_hold')
        self.assertEqual(self.service.refresh(first['attempt'])['state'],'waiting_idle')
        with self.assertRaises(DeveloperTestError):self.service.new_source(first['attempt'])
        self.assertEqual(self.hub.calls.count('source'),1)

    def test_real_archive_is_durable_before_ack_and_normal_cleanup_may_lag(self):
        first=self.start();self.hub.complete()
        result=self.service.refresh(first['attempt'])
        self.assertTrue(result['result_verified'])
        self.assertTrue(result['ack_confirmed'])
        self.assertFalse(result['status']['workspace_removed'])
        self.assertIsNone(result['status']['cleanup_after'])
        self.assertEqual(self.service.result_path(first['attempt']).read_bytes(),self.hub.archive)
        self.assertEqual(self.hub.calls.count('ack'),1)

    def test_lost_ack_reconciles_tombstone_after_restart_without_repeating_ack(self):
        first=self.start();self.hub.complete();self.hub.lose_ack=True
        result=self.service.refresh(first['attempt'])
        self.assertEqual(result['state'],'recovery_hold')
        reopened=GateService(self.root,transport=self.hub,inventory=lambda:self.inventory)
        self.hub.service=reopened
        result=reopened.refresh(first['attempt'])
        self.assertTrue(result['ack_confirmed'])
        self.assertEqual(self.hub.calls.count('submit'),1)
        self.assertEqual(self.hub.calls.count('ack'),1)
        self.assertEqual(self.hub.calls.count('result'),1)

    def test_changed_status_result_and_unconfirmed_release_refuse(self):
        first=self.start();self.hub.complete()
        self.hub.current['resource_release_confirmed']=False
        self.assertEqual(self.service.refresh(first['attempt'])['state'],'recovery_hold')
        self.assertNotIn('result',self.hub.calls)
        self.hub.current['resource_release_confirmed']=True
        self.service.refresh(first['attempt'])
        self.hub.current['result']['sha256']='b'*64
        self.assertEqual(self.service.refresh(first['attempt'])['state'],'recovery_hold')
        self.assertEqual(self.hub.calls.count('ack'),1)

    def test_paused_unknown_pause_stale_and_excluded_targets_never_submit(self):
        self.inventory['machines'][0]['node_paused']=None
        with self.assertRaises(DeveloperTestError):self.start()
        self.inventory['machines'][0]['node_paused']=False
        self.inventory['generated_at']=0
        with self.assertRaises(DeveloperTestError):self.start()
        self.inventory['generated_at']=time.time()
        pol=copy.deepcopy(self.policy);pol['revision']='excluded-policy';pol['excluded_targets']=pol['allowed_targets']
        self.service.save_policy(encoded({'expected_revision':self.policy['revision'],'policy':pol}))
        with self.assertRaises(DeveloperTestError):self.start()
        self.assertNotIn('submit',self.hub.calls)

    def test_campaign_cannot_change_payload_or_destination(self):
        self.start()
        changed=copy.deepcopy(self.request);changed['test_id']='different'
        with self.assertRaises(DeveloperTestError):self.service.start('c1','n1',encoded(changed))
        with self.assertRaises(DeveloperTestError):self.service.start('c2','n1',encoded(self.request))
        self.assertEqual(self.hub.calls.count('submit'),1)

    def test_fabricated_ack_cannot_delete_evidence_before_gateway_verification(self):
        first=self.start();self.hub.complete()
        self.hub.current.update(result_acknowledged=True,acknowledged_result=self.hub.current['result'],acknowledged_at=now_utc_str())
        self.assertEqual(self.service.refresh(first['attempt'])['state'],'recovery_hold')
        self.assertNotIn('ack',self.hub.calls)
        self.assertNotIn('result',self.hub.calls)

    def test_status_cannot_turn_a_failed_archive_into_success(self):
        from tests.gate_fixtures import make_result_tar_bytes
        first = self.start()
        self.hub.complete()
        self.hub.archive, _ = make_result_tar_bytes(self.request, self.cap, self.hub.acceptance, outcome='failed')
        self.hub.current['result'] = make_result_descriptor(self.hub.archive)
        result = self.service.refresh(first['attempt'])
        self.assertEqual(result['state'], 'recovery_hold')
        self.assertFalse(result['result_verified'])
        self.assertNotIn('ack', self.hub.calls)

    def test_terminal_outcome_cannot_change_after_archive_ack(self):
        first = self.start()
        self.hub.complete()
        self.assertTrue(self.service.refresh(first['attempt'])['ack_confirmed'])
        self.hub.current.update(state='failed', reason={'code': 'CHANGED', 'detail': 'Changed outcome.'})
        result = self.service.refresh(first['attempt'])
        self.assertEqual(result['state'], 'recovery_hold')
        self.assertEqual(result['status']['state'], 'passed')
        self.assertEqual(self.hub.calls.count('ack'), 1)

    def test_capability_reads_use_the_original_transport_pin(self):
        original = self.hub.json
        pins = []
        def observe(controller, node, operation, **kwargs):
            if operation == 'capabilities': pins.append(kwargs.get('pin'))
            return original(controller, node, operation, **kwargs)
        with patch.object(self.hub, 'json', side_effect=observe):
            first = self.start()
            path, _ = self.service.new_source(first['attempt'])
            archive, _ = make_source_tar_bytes()
            GateService._write_new(path, archive)
            self.service.upload(first['attempt'], path)
        self.assertEqual(pins, ['original-pin', 'original-pin'])

    def test_storage_failure_retains_hold_without_ack(self):
        first = self.start()
        self.hub.complete()
        for error in (OSError('fixture disk failure'), tarfile.ReadError('truncated fixture header')):
            with self.subTest(error=type(error).__name__), patch.object(self.hub, 'download', side_effect=error):
                result = self.service.refresh(first['attempt'])
            self.assertEqual(result['state'], 'recovery_hold')
            self.assertFalse(result['result_verified'])
            self.assertNotIn('ack', self.hub.calls)

    def test_invalid_result_never_acknowledges(self):
        first=self.start();self.hub.complete()
        self.hub.archive=b'invalid tar'
        self.hub.current['result']=make_result_descriptor(self.hub.archive)
        self.assertEqual(self.service.refresh(first['attempt'])['state'],'recovery_hold')
        self.assertNotIn('ack',self.hub.calls)

    def test_policy_stale_revision_and_duplicate_json_keys_refuse(self):
        with self.assertRaises(DeveloperTestError):
            self.service.save_policy(encoded({'expected_revision':'wrong','policy':self.policy}))
        with self.assertRaises(ValueError):
            self.service.save_policy(b'{"expected_revision":"a","expected_revision":"b","policy":{}}')


class GateOwnerRouteTests(unittest.TestCase):
    def test_remote_bare_gateway_key_cannot_read_or_submit_gate_jobs(self):
        from fastapi.testclient import TestClient
        from gateway import api
        with patch.dict(os.environ,{'GATEWAY_API_KEY':'fixture-gate-owner'}):
            client=TestClient(api.app,client=('remote.test',5000))
            for method,path in [('GET','/admin/api/developer-gates'),('POST','/admin/api/developer-gates/runs')]:
                result=client.request(method,path,headers={'Authorization':'Bearer fixture-gate-owner'},json={})
                self.assertEqual(result.status_code,403)

    def test_source_door_has_explicit_shared_ceiling(self):
        from gateway import api
        from gateway.gate_transport import SOURCE_CEILING
        self.assertEqual(api.body_ceiling_for('/admin/api/developer-gates/runs/a/source'),(SOURCE_CEILING,'route'))


if __name__=='__main__':unittest.main()
