"""Browser sign-in for /admin: password set from this Mac only, cookie session, backoff, CSRF.

Runs against a temp profile bound beyond loopback (as over Tailscale); never touches the
owner's real Gateway state or password.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-signin-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "0.0.0.0", "GATEWAY_API_KEY": "signin-fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin_session, api  # noqa: E402

KEY = {"Authorization": "Bearer signin-fixture-key"}
PHONE = ("100.64.0.9", 50000)
HOST = "testserver"
SAME = {"Origin": f"http://{HOST}"}


class BrowserSignIn(unittest.TestCase):
    def setUp(self) -> None:
        admin_session.remove_password()
        admin_session._failures.clear()
        self.phone = TestClient(api.app, client=PHONE)
        self.mac = TestClient(api.app, client=("127.0.0.1", 50001))

    def login(self, client, secret, headers=SAME):
        return client.post("/admin/login", data={"password": secret}, headers=headers, follow_redirects=False)

    def test_phone_gets_login_page_then_console_after_password(self):
        page = self.phone.get("/admin")
        self.assertIn('action="/admin/login"', page.text)
        self.assertIn("No browser password is set yet", page.text)
        self.assertEqual(self.phone.get("/admin/api/status").status_code, 401)

        # Only the app on this Mac, with the key, may set it.
        self.assertEqual(self.phone.put("/admin/api/settings/browser-password",
                                        json={"password": "correct horse"}, headers=KEY).status_code, 403)
        self.assertEqual(self.mac.put("/admin/api/settings/browser-password",
                                      json={"password": "correct horse"}).status_code, 403)
        self.assertEqual(self.mac.put("/admin/api/settings/browser-password",
                                      json={"password": "short"}, headers=KEY).status_code, 422)
        self.assertEqual(self.mac.put("/admin/api/settings/browser-password",
                                      json={"password": "correct horse"}, headers=KEY).status_code, 200)
        stored = (base / "admin-password.json").read_text()
        self.assertNotIn("correct horse", stored)

        self.assertEqual(self.login(self.phone, "wrong one").status_code, 401)
        ok = self.login(self.phone, "correct horse")
        self.assertEqual(ok.status_code, 303)
        cookie = ok.headers["set-cookie"].lower()
        for part in ("httponly", "samesite=strict", "path=/admin"):
            self.assertIn(part, cookie)
        self.assertNotIn("secure", cookie)
        self.assertIn('id="web-nav"', self.phone.get("/admin").text)
        self.assertEqual(self.phone.get("/admin/api/status").status_code, 200)

        # A signed-in browser cannot change the password, and writes need this origin.
        self.assertEqual(self.phone.delete("/admin/api/settings/browser-password").status_code, 403)
        self.assertEqual(self.phone.post("/admin/api/refresh-all",
                                         headers={"Origin": "http://evil.example"}).status_code, 403)

        # Sign out ends it server-side.
        token = self.phone.cookies.get(admin_session.COOKIE)
        self.assertEqual(self.phone.post("/admin/logout", headers=SAME).status_code, 200)
        self.assertFalse(admin_session.valid_session(token))

    def test_the_key_is_a_bearer_token_and_never_a_browser_password(self):
        """**Changed 2026-10-29.** The login form used to accept the gateway key, which minted
        an *owner* session — while `require_owner` deliberately refuses the bare key because
        Story Studio holds it. Any holder of the key could sign in and flip owner-only
        switches. The key stays a bearer token; the browser password is the only way in.
        """
        self.assertEqual(self.phone.get("/admin/api/status", headers=KEY).status_code, 200,
                         "the key still opens the API")
        # No password set: there is nothing to sign in with, and the page says so.
        self.assertEqual(self.login(self.phone, "signin-key-here").status_code, 403)
        self.assertNotIn(admin_session.COOKIE, self.phone.cookies)
        # With a password set, the key is simply the wrong password.
        admin_session.set_password("correct horse")
        self.assertEqual(self.login(self.phone, "signin-key-here").status_code, 401)
        self.assertNotIn(admin_session.COOKIE, self.phone.cookies)

    def test_cross_site_login_refused(self):
        self.assertEqual(self.login(self.phone, "signin-fixture-key",
                                    headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.login(self.phone, "signin-fixture-key", headers={}).status_code, 403)

    def test_failed_attempts_back_off_even_for_the_right_secret(self):
        admin_session.set_password("correct horse")
        for _ in range(admin_session.FREE_ATTEMPTS):
            self.assertEqual(self.login(self.phone, "nope").status_code, 401)
        self.assertEqual(self.login(self.phone, "correct horse").status_code, 429)

    def test_changing_the_password_signs_everyone_out(self):
        admin_session.set_password("correct horse")
        self.assertEqual(self.login(self.phone, "correct horse").status_code, 303)
        token = self.phone.cookies.get(admin_session.COOKIE)
        self.assertTrue(admin_session.valid_session(token))
        self.mac.put("/admin/api/settings/browser-password", json={"password": "another pass"}, headers=KEY)
        self.assertFalse(admin_session.valid_session(token))


if __name__ == "__main__":
    unittest.main()
