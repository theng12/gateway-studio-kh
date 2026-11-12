"""Automatic pairing: the endpoint file, its narrow token, and the switch that removes both."""
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests._isolation import sandbox_env

sandbox_env("gateway-pairing-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, config, extension_workers as ew, pairing  # noqa: E402

ON = {"GATEWAY_EXTENSION_WORKERS": "1", "GATEWAY_HOST": "127.0.0.1"}
LOOPBACK = ("127.0.0.1", 41001)
FRIEND = ("100.64.0.9", 41002)


class PairingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)
        ew.reset_for_tests()
        self.addCleanup(ew.reset_for_tests)
        for path in (pairing.state_path(), pairing.endpoint_path(),
                     config.APP_SUPPORT / "extension-workers.json"):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
        self.local = TestClient(api.app, client=LOOPBACK)
        self.friend = TestClient(api.app, client=FRIEND)

    def endpoint(self) -> dict:
        return json.loads(pairing.endpoint_path().read_text(encoding="utf-8"))

    def token(self) -> str:
        return self.endpoint()["pairingToken"]

    def pair(self, token=None, client=None, body=None):
        headers = {"Authorization": f"Bearer {token or self.token()}"}
        return (client or self.local).post(
            "/admin/api/extension-workers/pair",
            headers=headers, json=body or {"kind": "video", "worker_id": "chrome-paired"},
        )

    # -- the file ---------------------------------------------------------
    def test_on_by_default_and_written_on_launch(self):
        self.assertTrue(pairing.enabled(), "the switch starts on")
        pairing.sync_on_startup()
        row = self.endpoint()
        self.assertEqual(sorted(row), ["machineId", "pairingToken", "schema", "url"])
        self.assertEqual(row["schema"], 1)
        self.assertEqual(row["url"], f"http://127.0.0.1:{config.bind_port()}")
        self.assertTrue(row["machineId"])
        self.assertTrue(row["pairingToken"].startswith(pairing.TOKEN_PREFIX))
        self.assertEqual(oct(pairing.endpoint_path().stat().st_mode & 0o777), "0o600")
        # The plaintext token lives only in that 0600 file; the gateway keeps its digest.
        state = pairing.state_path().read_text(encoding="utf-8")
        self.assertNotIn(row["pairingToken"], state)
        self.assertNotIn("GATEWAY_API_KEY", json.dumps(row))

    def test_off_removes_the_file_and_revokes_the_token(self):
        pairing.sync_on_startup()
        old = self.token()
        self.assertTrue(pairing.endpoint_path().exists())
        answer = self.local.put("/admin/api/settings/pairing", json={"enabled": False})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertFalse(answer.json()["enabled"])
        self.assertFalse(pairing.endpoint_path().exists(), "off must remove the file")
        self.assertEqual(self.pair(old).status_code, 401, "a revoked token must fail")

        answer = self.local.put("/admin/api/settings/pairing", json={"enabled": True})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(pairing.endpoint_path().exists(), "on writes the file again")
        self.assertNotEqual(self.token(), old, "a new on period mints a new token")
        self.assertEqual(self.pair(old).status_code, 401)

    def test_switch_requires_an_explicit_boolean(self):
        answer = self.local.put("/admin/api/settings/pairing", json={"enabled": "yes"})
        self.assertEqual(answer.status_code, 422, answer.text)

    # -- the narrow token --------------------------------------------------
    def test_pairing_token_mints_a_worker_token_through_the_same_mint(self):
        pairing.sync_on_startup()
        answer = self.pair()
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertTrue(body["token"].startswith(ew.TOKEN_PREFIX))
        self.assertEqual(body["worker_id"], "chrome-paired")
        # …and the result is an ordinary worker token.
        worker = TestClient(api.app, client=FRIEND)
        self.assertEqual(worker.get("/api/workers/health",
                                    headers={"Authorization": f"Bearer {body['token']}"}).status_code, 200)

    def test_pairing_token_cannot_call_any_other_admin_route(self):
        pairing.sync_on_startup()
        token = self.token()
        headers = {"Authorization": f"Bearer {token}"}
        for method, path in (
            ("get", "/admin/api/status"),
            ("get", "/admin/api/extension-workers"),
            ("post", "/admin/api/extension-workers"),
            ("put", "/admin/api/settings/pairing"),
            ("get", "/admin/api/settings/pairing"),
            ("get", "/api/workers/health"),
        ):
            answer = getattr(self.friend, method)(path, headers=headers,
                                                  **({"json": {"kind": "video", "worker_id": "x"}}
                                                     if method in ("post", "put") else {}))
            # 503 is the gateway saying "no key configured beyond loopback" — still refused.
            self.assertIn(answer.status_code, (401, 403, 503),
                          f"{method} {path} answered {answer.status_code}")

    def test_gateway_key_is_not_a_pairing_token(self):
        pairing.sync_on_startup()
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "gw-key"}):
            self.assertEqual(self.pair("gw-key").status_code, 401)
            self.assertEqual(self.pair("ekw_wrong").status_code, 401)

    def test_pairing_is_loopback_only(self):
        pairing.sync_on_startup()
        self.assertEqual(self.pair(client=self.friend).status_code, 403)

    def test_pairing_route_is_404_when_browser_workers_are_off(self):
        pairing.sync_on_startup()
        with patch.dict(os.environ, {"GATEWAY_EXTENSION_WORKERS": "0"}):
            self.assertEqual(self.pair().status_code, 404)

    def test_the_switch_needs_the_app_on_this_mac(self):
        pairing.sync_on_startup()
        answer = self.friend.put("/admin/api/settings/pairing", json={"enabled": False})
        self.assertIn(answer.status_code, (401, 403))
        self.assertTrue(pairing.endpoint_path().exists())

    def test_no_file_is_written_when_the_gateway_does_not_listen_on_loopback(self):
        with patch.dict(os.environ, {"GATEWAY_HOST": "100.64.0.9"}):
            pairing.sync_on_startup()
            self.assertFalse(pairing.endpoint_path().exists())
            self.assertFalse(pairing.status()["token_live"])


if __name__ == "__main__":
    unittest.main()
