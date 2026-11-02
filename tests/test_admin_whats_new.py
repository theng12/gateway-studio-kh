"""The web page reads the current build's notes from the shared changelog."""
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env
_SANDBOX = sandbox_env()

from fastapi.testclient import TestClient
from gateway import __version__
from gateway.api import app
from gateway.admin import AdminError, whats_new


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
        self.assertIn('<div class="nav-title">Gateway Studio KH</div>', page)

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
