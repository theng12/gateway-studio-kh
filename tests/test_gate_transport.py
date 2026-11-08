"""The app-gate client is a bounded registered-controller transport, never a relay."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from tests._isolation import sandbox_env
PROFILE = sandbox_env('gate-transport-')

import httpx
from gateway.developer_contract import DeveloperTestError
from gateway.gate_transport import GateTransport


class Client(GateTransport):
    def identity(self, controller):
        if controller != 'c1':
            raise DeveloperTestError(404, 'UNKNOWN_TARGET', 'Not a registered controller.')
        return 'http://controller.test', 'fixture-secret-token', 'pin1'


class Chunks(httpx.SyncByteStream):
    def __iter__(self):
        yield b'prefix fixture-'
        yield b'secret-token suffix'


class GateTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, content=b'{}')
        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.http.close)
        self.client = Client(self.http)

    def test_only_seven_fixed_operations_and_original_auth(self):
        suffixes = {'capabilities':'/capabilities', 'submit':'', 'status':'/by-key/key1',
                    'cancel':'/by-key/key1/cancel', 'source':'/by-key/key1/source',
                    'ack':'/by-key/key1/ack'}
        for operation, suffix in suffixes.items():
            self.client.json('c1','n1',operation,key='key1',raw=b'{}' if operation in ('submit','cancel','ack') else None,pin='pin1')
            request=self.calls[-1]
            self.assertEqual(request.url.path, '/api/hub/fleet/nodes/n1/developer-gates'+suffix)
            self.assertEqual(request.headers['X-Hub-Token'],'fixture-secret-token')
            self.assertEqual(request.headers.get('Idempotency-Key'), 'key1' if operation=='submit' else None)
        self.assertEqual(len(self.client.OPERATIONS),7)
        with self.assertRaises(DeveloperTestError): self.client.json('c1','n1','shell')

    def test_changed_destination_and_unsafe_node_stop_before_wire(self):
        with self.assertRaises(DeveloperTestError): self.client.json('c1','n1','status',key='key1',pin='different')
        with self.assertRaises(DeveloperTestError): self.client.json('c1','../other','status',key='key1')
        self.assertEqual(self.calls,[])

    def test_oversized_json_and_secret_response_refuse(self):
        with self.assertRaises(DeveloperTestError): self.client.json('c1','n1','submit',key='key1',raw=b'12345',ceiling=4)
        self.assertEqual(self.calls,[])
        with self.assertRaises(DeveloperTestError): self.client.json('c1','n1','capabilities',ceiling=1)
        def secret(_): return httpx.Response(200,content=b'fixture-secret-token')
        with httpx.Client(transport=httpx.MockTransport(secret)) as http:
            with self.assertRaises(DeveloperTestError): Client(http).json('c1','n1','capabilities')

    def test_timeout_is_uncertain_and_no_retry(self):
        def timeout(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('fixture',request=request)
        with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
            with self.assertRaises(DeveloperTestError) as result:
                Client(http).json('c1','n1','submit',key='key1',raw=b'{}',pin='pin1')
        self.assertEqual(result.exception.code,'TRANSPORT_UNCERTAIN')
        self.assertEqual(len(self.calls),1)

    def test_source_body_streams_with_exact_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.tar';path.write_bytes(b'bytes')
            self.client.json('c1','n1','source',key='key1',source=path,pin='pin1')
        self.assertEqual(self.calls[0].read(),b'bytes')
        self.assertEqual(self.calls[0].headers['Content-Length'],'5')
        self.assertEqual(self.calls[0].headers['Content-Type'],'application/x-tar')

    def test_result_is_exclusive_durable_and_hash_bound(self):
        content=b'fixture archive'
        descriptor={'size_bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=content))) as http:
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'result.partial'
                Client(http).download('c1','n1','key1',descriptor,path,pin='pin1',ceiling=100)
                self.assertEqual(path.read_bytes(),content)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                with self.assertRaises(FileExistsError): Client(http).download('c1','n1','key1',descriptor,path,pin='pin1',ceiling=100)

    def test_secret_split_between_chunks_never_becomes_result(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Chunks()))) as http:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(DeveloperTestError):
                    Client(http).download('c1','n1','key1',{'size_bytes':100,'sha256':'a'*64},Path(tmp)/'part',pin='pin1',ceiling=100)

    def test_redirect_never_forwards_credentials_to_another_destination(self):
        def redirect(request):
            self.calls.append(request)
            return httpx.Response(302,headers={'Location':'http://elsewhere.test/result'})
        with httpx.Client(transport=httpx.MockTransport(redirect)) as http:
            with self.assertRaises(DeveloperTestError): Client(http).json('c1','n1','capabilities')
        self.assertEqual(len(self.calls),1)


if __name__=='__main__': unittest.main()
