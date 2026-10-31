import hashlib
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='guest-voices-')
BASE = Path(TEMP.name)
os.environ.update({'GATEWAY_APP_SUPPORT': str(BASE), 'GATEWAY_DATA': str(BASE / 'data'),
                   'GATEWAY_DB': str(BASE / 'db.sqlite'), 'GATEWAY_VOICES': str(BASE / 'voices.json'),
                   'GATEWAY_ENV_FILE': str(BASE / '.env'), 'GATEWAY_FLEET': str(BASE / 'fleet.json'),
                   'STUDIOHUB_READY_FILE': str(BASE / 'no-hub'), 'GATEWAY_NO_MIGRATE': '1',
                   'GATEWAY_HOST': '127.0.0.1', 'GATEWAY_API_KEY': 'test-only-key'})
from fastapi.testclient import TestClient
from gateway import admin_session, api, voice_import, voices


def wav(seed=''):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(hashlib.sha256(seed.encode()).digest() * 100)
    return out.getvalue()


class GuestVoices(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, client=('100.1.2.3', 1234))
        self.addCleanup(self.client.close)
        for name in ('Alice', 'Bob'):
            row = admin_session.add_guest(name + self._testMethodName, 'password123')
            token = admin_session.create_session(role='guest', guest_id=row['id'], name=row['name'])
            setattr(self, name.lower(), {admin_session.COOKIE: token})
        self.headers = {'Origin': 'http://testserver'}
        self.patch = patch('gateway.voice_import.guest_transcribe')
        self.patch.start(); self.addCleanup(self.patch.stop)

    def add(self, data=None, extra=None):
        fields = {'name': 'សំឡេង', 'language': 'km', 'permission': 'true', **(extra or {})}
        return self.client.post('/admin/api/create/voices', cookies=self.alice, headers=self.headers,
                                data=fields, files={'file': ('ignored.wav', data or wav(self._testMethodName), 'audio/wav')})

    def test_isolation_preview_and_restore(self):
        response = self.add(); self.assertEqual(response.status_code, 200, response.text)
        voice_id = response.json()['id']
        self.assertIn(voice_id, [v['id'] for v in self.client.get('/admin/api/voices', cookies=self.alice).json()['voices']])
        self.assertNotIn(voice_id, [v['id'] for v in self.client.get('/admin/api/voices', cookies=self.bob).json()['voices']])
        self.assertEqual(self.client.get('/admin/api/create/voices/' + voice_id + '/preview', cookies=self.bob).status_code, 404)
        with patch.object(voices.VoiceRegistry, '_synth_preview', side_effect=AssertionError('paid')):
            self.assertEqual(self.client.get('/admin/api/create/voices/' + voice_id + '/preview', cookies=self.alice).status_code, 200)
        self.assertEqual(self.client.post('/admin/api/create/voices/remove', cookies=self.bob, headers=self.headers, json={'voice_id': voice_id}).status_code, 404)
        self.assertEqual(self.client.post('/admin/api/create', cookies=self.bob, headers=self.headers, json={'modality':'tts','model_id':'x','params':{'voice_id':voice_id}}).status_code, 404)
        self.assertNotIn(voice_id, [v['id'] for v in self.client.get('/v1/voices', headers={'Authorization': 'Bearer ' + os.environ['GATEWAY_API_KEY']}).json()['voices']])
        payload = voice_import._read_catalog()
        entry = next(v for v in payload['voices'] if v['id'] == voice_import.catalogue_id(voice_id))
        entry['guest_status'] = 'ready'; entry['reference_segments'] = [{'start': 0, 'end': 1, 'text': 'hello'}]
        voice_import._write_catalog(payload)
        self.assertTrue(voices.voice_registry().set_enabled(voice_id, True))
        self.assertIn(voice_id, [v['id'] for v in self.client.get('/v1/voices', headers={'Authorization': 'Bearer ' + os.environ['GATEWAY_API_KEY']}).json()['voices']])
        result = self.client.post('/admin/api/create/voices/remove', cookies=self.alice, headers=self.headers, json={'voice_id': voice_id})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue((voice_import.backup_dir() / result.json()['backup']).is_dir())

    def test_refusals(self):
        self.assertEqual(self.add(extra={'_reference_path':'/etc/passwd'}).status_code, 422)
        self.assertEqual(self.add(data=b'not audio').status_code, 422)
        self.assertEqual(self.add(extra={'permission':'false'}).status_code, 422)
        with patch.object(admin_session, 'same_origin', return_value=True), patch('gateway.admin.guest_voice_limits', return_value={'max_clip_bytes': 2, 'max_voices': 1}):
            self.assertEqual(self.add().status_code, 413)

    def test_generation_and_limits(self):
        result = self.add(); self.assertEqual(result.status_code, 200, result.text)
        voice_id = result.json()['id']
        payload = voice_import._read_catalog()
        entry = next(v for v in payload['voices'] if v['id'] == voice_import.catalogue_id(voice_id))
        entry['guest_status'] = 'ready'; entry['reference_segments'] = [{'start': 0, 'end': 1, 'text': 'hello'}]
        voice_import._write_catalog(payload)
        with patch('gateway.api._submit', return_value=({'id': 'job'}, True)) as submit, patch('gateway.api._job_envelope', return_value={'id': 'job'}):
            response = self.client.post('/admin/api/create', cookies=self.alice, headers=self.headers,
                                        json={'modality': 'tts', 'model_id': 'local::clone', 'params': {'voice_id': voice_id, 'text': 'សួស្តី'}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(submit.call_args.args[2]['voice_id'], voice_id)
        with patch('gateway.admin.guest_voice_limits', return_value={'max_clip_bytes': 25000000, 'max_voices': 1}):
            self.assertEqual(self.add(data=wav('different')).status_code, 409)

    def test_failed_transcription_is_persisted(self):
        response = self.add(); self.assertEqual(response.status_code, 200, response.text)
        raw = voice_import.catalogue_id(response.json()['id'])
        with patch('gateway.voice_import.annotation_targets', return_value=[]):
            self.patch.stop(); voice_import.guest_transcribe(raw); self.patch.start()
        entry = next(v for v in voice_import.voices() if v['id'] == raw)
        self.assertEqual(entry['guest_status'], 'failed')
        self.assertIn('No Studio Hub', entry['guest_error'])
