"""One advertised pending Chat request, separate from execution capacity."""
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import httpx
_STATE=tempfile.mkdtemp(prefix='gateway-chat-priority-')
os.environ.update(GATEWAY_APP_SUPPORT=_STATE,GATEWAY_DATA=_STATE+'/data',GATEWAY_NO_MIGRATE='1')
from gateway import concurrency, fleet
from gateway.worker import Worker
from gateway import worker as worker_module
from gateway.adapters import studio_hub
from gateway.adapters.base import BackendError, BackendRequest, requests_started


class ChatQueueAheadTests(unittest.TestCase):
    def test_restart_holds_chat_without_resending_or_treating_it_as_a_batch(self):
        held=[]
        job={'id':'job','state':'running','modality':'chat','upstream_job_id':'chat:'+'a'*32}
        store=SimpleNamespace(interrupted=['job'],get=lambda _:job,hold_interrupted=held.append)
        worker=Worker(store)
        with patch.object(worker_module,'adapter_for',side_effect=AssertionError('must not resume Chat')):
            worker._resume_interrupted()
        self.assertEqual(held,['job'])
        self.assertEqual(worker.active(),0)

    def test_unknown_chat_hold_never_uses_a_batch_404_as_proof_of_no_work(self):
        closed=[]
        job={'id':'job','provider_id':'hub','modality':'chat','upstream_job_id':'chat:'+'a'*32}
        store=SimpleNamespace(holds=lambda **_: [job],close_hold=lambda *args,**kwargs:closed.append(args))
        definition=SimpleNamespace(id='hub',kind='studio_hub')
        with patch.object(worker_module.registry,'definitions',return_value=[definition]), \
             patch.object(worker_module.registry,'definition',return_value=definition), \
             patch.object(studio_hub,'batch_snapshot',side_effect=AssertionError('must not read batch route')):
            result=worker_module.reconcile_holds(store)
        self.assertEqual(result['still_open'],1)
        self.assertEqual(closed,[])

    def test_one_pending_request_while_one_chat_is_running(self):
        provider=fleet.PROVIDER_PREFIX+'site'
        row={'provider_id':provider,'reachable':True,'capacity':{'by_operation':{
            'chat.completion':{'pending_request_slots':1,'pending_queue_wait_seconds':10800,
                               'pending_request_models':['model']}}}}
        worker=Worker(object())
        worker._per_lane[(provider,'chat')]=1
        worker._per_model[(provider,'chat','model')]=1
        budget=concurrency.Budget(limit=1,source=concurrency.SOURCE_HUB,detail='one Mac')
        with patch('gateway.fleet.snapshot',return_value={'locations':[row]}), \
             patch('gateway.fleet.is_paused_provider',return_value=False), \
             patch('gateway.concurrency.stated_provider_limit',return_value=None):
            self.assertEqual(worker._room(provider,'chat','model',budget,{'model':1}),1)
            worker._per_lane[(provider,'chat')]=2
            worker._per_model[(provider,'chat','model')]=2
            self.assertEqual(worker._room(provider,'chat','model',budget,{'model':1}),0)
            self.assertEqual(worker._room(provider,'tts','model',budget,{'model':1}),1)
            row['capacity']['by_operation']['chat.completion'].clear()
            worker._per_lane[(provider,'chat')]=1
            worker._per_model[(provider,'chat','model')]=1
            self.assertEqual(worker._room(provider,'chat','model',budget,{'model':1}),0)

    def test_status_failure_preserves_the_one_completion_and_send_evidence(self):
        released=threading.Event()
        answer=httpx.Response(200,json={'choices':[{'message':{'content':'answer'},'finish_reason':'length'}],
                                        'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}})
        posts=[]
        def post(url,**kwargs):
            posts.append((url,kwargs))
            released.wait(2)
            return answer
        def get(*args,**kwargs):
            released.set()
            raise httpx.ReadTimeout('status unavailable')
        waiting=[]
        handles=[]
        request=BackendRequest(provider_id='hub',model_id='model',modality='chat',
                               params={'input':[{'role':'user','content':'hi'}],'_client_request_id':'test'},
                               values={'hub_token':'test'},base_url='http://fixture',
                               checkpoint=handles.append,on_wait=waiting.append)
        before=requests_started()
        with patch.object(studio_hub,'chat_queue_wait_seconds',return_value=2), \
             patch.object(studio_hub,'lane_deadline_seconds',return_value=3), \
             patch.object(studio_hub,'POLL_INTERVAL_S',0.01), \
             patch.object(studio_hub.httpx,'post',side_effect=post), \
             patch.object(studio_hub.httpx,'get',side_effect=get):
            result=studio_hub._generate_chat(request)
        self.assertEqual(result.text,'answer')
        self.assertEqual(result.payload['finish_reason'],'length')
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][1]['timeout'].read,15)
        self.assertEqual(len(handles),1)
        self.assertTrue(handles[0].startswith('chat:'))
        self.assertIsNotNone(waiting[0])
        self.assertIsNone(waiting[-1])
        self.assertGreater(requests_started(),before)

    def test_read_timeout_stays_ambiguous_after_checkpoint(self):
        request=BackendRequest(provider_id='hub',model_id='model',modality='chat',
                               params={'input':[{'role':'user','content':'hi'}]},values={'hub_token':'test'},
                               base_url='http://fixture',checkpoint=lambda _:None)
        with patch.object(studio_hub,'chat_queue_wait_seconds',return_value=2), \
             patch.object(studio_hub.httpx,'post',side_effect=httpx.ReadTimeout('uncertain')):
            with self.assertRaises(BackendError) as raised:
                studio_hub._generate_chat(request)
        self.assertIsNone(raised.exception.upstream_accepted)
        self.assertFalse(raised.exception.upstream_settled)

    def test_queued_machine_pause_is_settled_not_an_unknown_hold(self):
        request=BackendRequest(provider_id='hub',model_id='model',modality='chat',
                               params={'input':[{'role':'user','content':'hi'}]},values={'hub_token':'test'},
                               base_url='http://fixture',checkpoint=lambda _:None)
        with patch.object(studio_hub,'chat_queue_wait_seconds',return_value=2), \
             patch.object(studio_hub.httpx,'post',return_value=httpx.Response(409,json={
                 'code':'MACHINE_DISABLED','detail':'paused while waiting'})):
            with self.assertRaises(BackendError) as raised:
                studio_hub._generate_chat(request)
        self.assertFalse(raised.exception.upstream_accepted)
        self.assertTrue(raised.exception.upstream_settled)

if __name__=='__main__':
    unittest.main()
