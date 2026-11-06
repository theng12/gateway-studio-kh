"""The web page reads the current build's notes from the shared changelog."""
import subprocess
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env
_SANDBOX = sandbox_env()

from fastapi.testclient import TestClient
from gateway import __version__
from gateway.api import app
from gateway.admin import AdminError, computer_name, whats_new


class AdminWhatsNewTest(unittest.TestCase):
    def test_current_version_and_changes_are_served(self):
        response = TestClient(app, client=('127.0.0.1', 50000)).get('/admin/api/whats-new')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['version'], __version__)
        self.assertTrue(data['release'].startswith(__version__ + ' — '))
        self.assertTrue(data['changes'])
        self.assertTrue(all(isinstance(change, str) and change.strip() for change in data['changes']))
        page = TestClient(app, client=('127.0.0.1', 50000)).get('/admin').text
        self.assertIn('<title>Gateway Studio KH', page)
        self.assertIn('<div class="nav-title">Gateway Studio KH'
                      '<span class="nav-machine" id="nav-machine" hidden></span></div>', page)

    def tearDown(self):
        computer_name.cache_clear()

    def test_status_version_matches_current_notes(self):
        client = TestClient(app, client=('127.0.0.1', 50000))
        response = client.get('/admin/api/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], client.get('/admin/api/whats-new').json()['version'])
        page = client.get('/admin').text
        self.assertIn('id="app-version"', page)
        self.assertIn('onclick="openNav(\'whatsnew\')"', page)

    def test_status_publishes_the_computer_name_read_once(self):
        computer_name.cache_clear()
        done = subprocess.CompletedProcess([], 0, stdout='terranash-1301\n', stderr='')
        with patch('gateway.admin.subprocess.run', return_value=done) as run:
            client = TestClient(app, client=('127.0.0.1', 50000))
            first = client.get('/admin/api/status').json()
            second = client.get('/admin/api/status').json()
        self.assertEqual(first['computer_name'], 'terranash-1301')
        self.assertEqual(second['computer_name'], 'terranash-1301')
        self.assertEqual(run.call_count, 1, 'the name is read once, not on every status poll')

    def test_status_says_when_the_computer_name_is_unavailable(self):
        failures = {
            'scutil will not start': OSError('no scutil'),
            'scutil refuses': subprocess.CompletedProcess([], 1, stdout='', stderr='nope'),
            'scutil says nothing': subprocess.CompletedProcess([], 0, stdout=' \n', stderr=''),
        }
        for reason, result in failures.items():
            with self.subTest(reason), patch('gateway.admin.subprocess.run') as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                computer_name.cache_clear()
                data = TestClient(app, client=('127.0.0.1', 50000)).get('/admin/api/status').json()
                self.assertIsNone(data['computer_name'])

    def test_missing_notes_fail_loudly(self):
        with patch('gateway.admin.Path.read_text', side_effect=OSError('missing')):
            with self.assertRaises(AdminError) as caught:
                whats_new()
        self.assertEqual(caught.exception.status, 503)

    def test_another_versions_notes_are_not_shown_as_this_build(self):
        with patch('gateway.admin.Path.read_text', return_value='## 0.0.0 — 2020-01-01\n- Old\n'):
            with self.assertRaises(AdminError) as caught:
                whats_new()
        self.assertEqual(caught.exception.status, 503)


if __name__ == '__main__':
    unittest.main()
