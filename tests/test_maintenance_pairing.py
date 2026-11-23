"""ASGI integration tests for maintenance hold-token verification (0.2.262).

Tests the full route-level auth boundary via FastAPI TestClient: valid token
accepts drain/resume, invalid/missing/malformed/rotated tokens refuse without
side effects, non-loopback/forwarded/Origin headers refuse, scope isolation
proves hold-token cannot access unrelated admin routes, and drain parameter
clamping enforces 20 s default / 30 s max timeout with 0 preserved for renewal.

Uses TemporaryDirectory for all fixtures (no manual cleanup). Runs with the
canonical .venv/bin/python from the repo root with PYTHONPATH pointing at this
worktree so imports resolve here, not in the canonical checkout.

    cd /path/to/gateway-studio-kh/repository
    .venv/bin/python /path/to/worktree/tests/test_maintenance_pairing.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Isolation: point every gateway path at a fresh temp dir BEFORE any import.
# This must happen before `from gateway import ...` so the first import binds
# to our sandbox, not the owner's real Application Support.
# ---------------------------------------------------------------------------
_SANDBOX = tempfile.TemporaryDirectory(prefix="gateway-maint-asgi-")
_ROOT = Path(_SANDBOX.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_ROOT),
    "GATEWAY_DATA": str(_ROOT / "data"),
    "GATEWAY_DB": str(_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_ROOT / ".env"),
    "GATEWAY_ENDPOINT_FILE": str(_ROOT / "gateway-endpoint.json"),
    "GATEWAY_FLEET": str(_ROOT / "fleet.json"),
    "GATEWAY_VOICES": str(_ROOT / "voices.json"),
    "GATEWAY_VOICES_OUT": str(_ROOT / "voices.json"),
    "STUDIOHUB_READY_FILE": str(_ROOT / "no-hub.json"),
    "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1",
    "GATEWAY_QUEUE_CLAIM_WAIT_S": "0",
})

# Token fixture: a separate TemporaryDirectory so it survives across test classes
_TOKEN_DIR = tempfile.TemporaryDirectory(prefix="gateway-maint-token-")
_TOKEN_FILE = Path(_TOKEN_DIR.name) / "hold-token"
_VALID_TOKEN = "a" * 64
_TOKEN_FILE.write_text(_VALID_TOKEN + "\n", encoding="utf-8")
os.chmod(_TOKEN_FILE, 0o600)
os.environ["KHU_HOLD_TOKEN_FILE"] = str(_TOKEN_FILE)

# Ensure worktree source is imported, not the canonical checkout
_WORKTREE = Path(__file__).resolve().parents[1]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from fastapi.testclient import TestClient  # noqa: E402
from gateway import api, maintenance  # noqa: E402
from gateway.runtime import worker as live_worker  # noqa: E402


def _both_headers(token: str = _VALID_TOKEN) -> dict[str, str]:
    """The exact header pair the real Launcher sends."""
    return {"X-Hub-Token": token, "Authorization": f"Bearer {token}"}


class TestValidTokenAcceptsDrainAndResume(unittest.TestCase):
    """A valid hold-token on direct loopback authorizes POST and DELETE."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 60001))
        self.addCleanup(self.client.close)
        self.addCleanup(live_worker.resume_dispatch)

    def test_post_drain_returns_200_with_correct_defaults(self) -> None:
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain", headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["unsettled"], [])
        # Default timeout is 20 (not 15), pause is 120
        mock_drain.assert_called_once()
        args = mock_drain.call_args
        self.assertEqual(args[0][0], 20.0, "default timeout must be 20 s")
        self.assertEqual(args[1].get("pause_s", args[0][1] if len(args[0]) > 1 else None), 120.0)

    def test_loopback_token_works_when_gateway_is_lan_bound(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_HOST": "0.0.0.0"}), \
             patch.object(live_worker, "drain", return_value=[]) as drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain", headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        drain.assert_called_once()

    def test_delete_resume_returns_200(self) -> None:
        with patch.object(live_worker, "resume_dispatch") as mock_resume, \
             patch.object(live_worker, "dispatch_paused", return_value=False):
            resp = self.client.delete("/admin/api/maintenance/drain", headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        mock_resume.assert_called_once()

    def test_timeout_zero_preserved_for_renewal(self) -> None:
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain",
                                    params={"timeout_s": 0},
                                    headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(mock_drain.call_args[0][0], 0.0,
                         "timeout_s=0 must be preserved for Launcher renewal")

    def test_timeout_capped_at_30(self) -> None:
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain",
                                    params={"timeout_s": 60},
                                    headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(mock_drain.call_args[0][0], 30.0)

    def test_nan_timeout_becomes_default(self) -> None:
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain",
                                    params={"timeout_s": "nan"},
                                    headers=_both_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(mock_drain.call_args[0][0], 20.0)


class TestMissingInvalidMalformedTokensRefuse(unittest.TestCase):
    """Every invalid credential form returns 401 and never calls drain/resume."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 60002))
        self.addCleanup(self.client.close)

    def _assert_refused(self, method: str, **kwargs) -> None:
        with patch.object(live_worker, "drain") as mock_drain, \
             patch.object(live_worker, "resume_dispatch") as mock_resume:
            resp = getattr(self.client, method)("/admin/api/maintenance/drain", **kwargs)
        self.assertIn(resp.status_code, (401, 403), f"Expected refusal, got {resp.status_code}: {resp.text}")
        mock_drain.assert_not_called()
        mock_resume.assert_not_called()

    def test_no_headers_at_all(self) -> None:
        self._assert_refused("post")

    def test_wrong_token(self) -> None:
        wrong = "b" * 64
        self._assert_refused("post", headers=_both_headers(wrong))

    def test_short_token(self) -> None:
        self._assert_refused("post", headers=_both_headers("abcd1234"))

    def test_long_token(self) -> None:
        self._assert_refused("post", headers=_both_headers("a" * 128))

    def test_non_hex_token(self) -> None:
        self._assert_refused("post", headers=_both_headers("z" * 64))

    def test_non_ascii_token_refused_at_unit_level(self) -> None:
        """Non-ASCII tokens are refused by maintenance.verify() directly.

        httpx/starlette refuse non-ASCII header values before they reach the
        server, so this cannot be tested at the ASGI level. The unit-level
        check proves the guard handles it without raising.
        """
        from types import SimpleNamespace
        req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        # Non-ASCII in hub_token position
        self.assertFalse(maintenance.verify(None, "\xff" * 64, req))
        # Non-ASCII in bearer position
        self.assertFalse(maintenance.verify("Bearer " + "\xff" * 64, None, req))

    def test_conflicting_headers(self) -> None:
        other = "c" * 64
        self._assert_refused("post", headers={"X-Hub-Token": _VALID_TOKEN,
                                               "Authorization": f"Bearer {other}"})

    def test_only_x_hub_token_accepted(self) -> None:
        """X-Hub-Token alone (no Authorization) should still work."""
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain",
                                    headers={"X-Hub-Token": _VALID_TOKEN})
        self.assertEqual(resp.status_code, 200, resp.text)
        mock_drain.assert_called_once()

    def test_only_authorization_bearer_accepted(self) -> None:
        """Authorization: Bearer alone (no X-Hub-Token) should still work."""
        with patch.object(live_worker, "drain", return_value=[]) as mock_drain, \
             patch.object(live_worker, "active", return_value=0):
            resp = self.client.post("/admin/api/maintenance/drain",
                                    headers={"Authorization": f"Bearer {_VALID_TOKEN}"})
        self.assertEqual(resp.status_code, 200, resp.text)
        mock_drain.assert_called_once()

    def test_missing_file_refuses(self) -> None:
        prev = os.environ.get("KHU_HOLD_TOKEN_FILE")
        os.environ["KHU_HOLD_TOKEN_FILE"] = str(_ROOT / "nonexistent-token")
        try:
            self._assert_refused("post", headers=_both_headers())
        finally:
            if prev is not None:
                os.environ["KHU_HOLD_TOKEN_FILE"] = prev
            else:
                os.environ.pop("KHU_HOLD_TOKEN_FILE", None)

    def test_rotated_token_refuses(self) -> None:
        """Replacing the file content invalidates the old token immediately."""
        new_token = "d" * 64
        _TOKEN_FILE.write_text(new_token + "\n", encoding="utf-8")
        os.chmod(_TOKEN_FILE, 0o600)
        try:
            self._assert_refused("post", headers=_both_headers(_VALID_TOKEN))
        finally:
            _TOKEN_FILE.write_text(_VALID_TOKEN + "\n", encoding="utf-8")
            os.chmod(_TOKEN_FILE, 0o600)

    def test_wrong_mode_file_refuses(self) -> None:
        os.chmod(_TOKEN_FILE, 0o644)
        try:
            self._assert_refused("post", headers=_both_headers())
        finally:
            os.chmod(_TOKEN_FILE, 0o600)

    def test_malformed_bearer_cannot_hide_behind_valid_hub_header(self) -> None:
        self._assert_refused("post", headers={"X-Hub-Token": _VALID_TOKEN,
                                             "Authorization": f"Token {_VALID_TOKEN}"})

    def test_malformed_hub_cannot_hide_behind_valid_bearer(self) -> None:
        self._assert_refused("post", headers={"X-Hub-Token": "bad",
                                             "Authorization": f"Bearer {_VALID_TOKEN}"})

    def test_cookie_is_refused_even_with_valid_token(self) -> None:
        self._assert_refused("post", headers={**_both_headers(), "Cookie": "session=irrelevant"})

    def test_query_credentials_are_refused_even_with_valid_headers(self) -> None:
        self._assert_refused("post", headers=_both_headers(), params={"token": _VALID_TOKEN})

    def test_duplicate_headers_are_refused(self) -> None:
        self._assert_refused("post", headers=[("X-Hub-Token", _VALID_TOKEN),
                                             ("X-Hub-Token", "b" * 64)])

    def test_invalid_delete_never_resumes(self) -> None:
        self._assert_refused("delete", headers=_both_headers("b" * 64))


class TestNonLoopbackForwardedOriginRefuse(unittest.TestCase):
    """Network boundary enforcement: non-loopback, forwarded, Origin all refused."""

    def setUp(self) -> None:
        self.addCleanup(live_worker.resume_dispatch)

    def _refused_from(self, host: str, **extra_headers) -> None:
        client = TestClient(api.app, client=(host, 60010))
        self.addCleanup(client.close)
        headers = _both_headers()
        headers.update(extra_headers)
        with patch.object(live_worker, "drain") as mock_drain:
            resp = client.post("/admin/api/maintenance/drain", headers=headers)
        self.assertIn(resp.status_code, (401, 403))
        mock_drain.assert_not_called()

    def test_non_loopback_ip_refused(self) -> None:
        self._refused_from("192.168.1.5")

    def test_forwarded_header_refused(self) -> None:
        self._refused_from("127.0.0.1", forwarded="for=10.0.0.1")

    def test_x_forwarded_for_refused(self) -> None:
        self._refused_from("127.0.0.1", **{"x-forwarded-for": "10.0.0.1"})

    def test_x_forwarded_proto_refused(self) -> None:
        self._refused_from("127.0.0.1", **{"x-forwarded-proto": "https"})

    def test_origin_header_refused(self) -> None:
        self._refused_from("127.0.0.1", origin="http://evil.example.com")

    def test_null_origin_refused(self) -> None:
        self._refused_from("127.0.0.1", origin="null")

    def test_empty_origin_refused(self) -> None:
        self._refused_from("127.0.0.1", origin="")


class TestScopeIsolation(unittest.TestCase):
    """Hold-token must NOT authorize unrelated admin/guest/local-app routes.

    These are actual ASGI requests proving the token has no scope beyond
    POST/DELETE /admin/api/maintenance/drain.
    """

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 60020))
        self.addCleanup(self.client.close)

    def test_hold_token_cannot_access_status(self) -> None:
        with patch("gateway.admin.bearer_guest", return_value=None), \
             patch("gateway.admin.admin_session.session", return_value=None):
            resp = self.client.get("/admin/api/status", headers=_both_headers())
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_hold_token_cannot_access_status_from_non_loopback(self) -> None:
        client = TestClient(api.app, client=("192.168.1.5", 60021))
        self.addCleanup(client.close)
        resp = client.get("/admin/api/status", headers=_both_headers())
        self.assertIn(resp.status_code, (401, 403, 503),
                      f"Hold-token must not grant admin status access: {resp.status_code}")

    def test_hold_token_cannot_access_pairing_settings(self) -> None:
        """PUT /admin/api/settings/pairing uses require_local_app — hold-token must not bypass it."""
        client = TestClient(api.app, client=("192.168.1.5", 60022))
        self.addCleanup(client.close)
        resp = client.put("/admin/api/settings/pairing",
                          json={"enabled": True},
                          headers=_both_headers())
        self.assertIn(resp.status_code, (401, 403, 503),
                      f"Hold-token must not grant pairing settings access: {resp.status_code}")

    def test_hold_token_cannot_access_whats_new_from_non_loopback(self) -> None:
        client = TestClient(api.app, client=("192.168.1.5", 60023))
        self.addCleanup(client.close)
        resp = client.get("/admin/api/whats-new", headers=_both_headers())
        self.assertIn(resp.status_code, (401, 403, 503),
                      f"Hold-token must not grant whats-new access: {resp.status_code}")

    def test_hold_credentials_remain_scoped_on_loopback(self) -> None:
        for headers in (_both_headers(), {"X-Hub-Token": _VALID_TOKEN},
                        {"Authorization": f"Bearer {_VALID_TOKEN}"}):
            with self.subTest(headers=list(headers)):
                resp = self.client.get("/admin/api/status", headers=headers)
                self.assertEqual(resp.status_code, 401, resp.text)
                self.assertEqual(resp.json()["code"], "MAINTENANCE_SCOPE_REFUSED")

    def test_loopback_pairing_change_is_refused_before_side_effect(self) -> None:
        with patch("gateway.pairing.set_enabled") as change:
            resp = self.client.put("/admin/api/settings/pairing", json={"enabled": True},
                                   headers=_both_headers())
        self.assertEqual(resp.status_code, 401, resp.text)
        change.assert_not_called()

    def test_hold_token_cannot_use_guest_create_routes(self) -> None:
        resp = self.client.get("/admin/api/create/models", headers=_both_headers())
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_explicit_gateway_owner_key_keeps_its_existing_access(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "e" * 64}):
            resp = self.client.get("/admin/api/status",
                                   headers={"Authorization": f"Bearer {'e' * 64}"})
        self.assertEqual(resp.status_code, 200, resp.text)


class TestUnsafeTokenFiles(unittest.TestCase):
    def _refused_path(self, path: Path) -> None:
        with patch.dict(os.environ, {"KHU_HOLD_TOKEN_FILE": str(path)}), \
             TestClient(api.app, client=("127.0.0.1", 60030)) as client, \
             patch.object(live_worker, "drain") as drain:
            response = client.post("/admin/api/maintenance/drain", headers=_both_headers())
        self.assertEqual(response.status_code, 401, response.text)
        drain.assert_not_called()

    def test_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "link"
            link.symlink_to(_TOKEN_FILE)
            self._refused_path(link)

    def test_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._refused_path(Path(directory))

    def test_fifo_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            self._refused_path(fifo)

    def test_oversized_and_non_hex_files_are_refused(self) -> None:
        for text in (_VALID_TOKEN + " " * 1000, "+" + "a" * 63, "a_" * 32, "z" * 64):
            with self.subTest(size=len(text)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "token"
                path.write_text(text, encoding="ascii")
                os.chmod(path, 0o600)
                self._refused_path(path)

    def test_binary_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_bytes(b"\xff" * 64)
            os.chmod(path, 0o600)
            self._refused_path(path)

    def test_wrong_owner_is_refused(self) -> None:
        with patch("gateway.maintenance.os.geteuid", return_value=os.geteuid() + 1):
            self.assertIsNone(maintenance._read_token_file())

    def test_atomic_replacement_accepts_new_and_refuses_old(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(_VALID_TOKEN, encoding="ascii")
            os.chmod(path, 0o600)
            replacement = Path(directory) / "replacement"
            replacement.write_text("f" * 64, encoding="ascii")
            os.chmod(replacement, 0o600)
            with patch.dict(os.environ, {"KHU_HOLD_TOKEN_FILE": str(path)}), \
                 TestClient(api.app, client=("127.0.0.1", 60031)) as client, \
                 patch.object(live_worker, "drain", return_value=[]) as drain, \
                 patch.object(live_worker, "active", return_value=0):
                self.assertEqual(client.post("/admin/api/maintenance/drain",
                                             headers=_both_headers()).status_code, 200)
                os.replace(replacement, path)
                self.assertEqual(client.post("/admin/api/maintenance/drain",
                                             headers=_both_headers()).status_code, 401)
                self.assertEqual(client.post("/admin/api/maintenance/drain",
                                             headers=_both_headers("f" * 64)).status_code, 200)
                self.assertEqual(drain.call_count, 2)


class TestClampDrainParams(unittest.TestCase):
    """Unit tests for parameter clamping logic."""

    def test_defaults(self) -> None:
        t, p = maintenance.clamp_drain_params(20.0, 120.0)
        self.assertEqual(t, 20.0)
        self.assertEqual(p, 120.0)

    def test_timeout_capped_at_30(self) -> None:
        t, _ = maintenance.clamp_drain_params(60.0, 120.0)
        self.assertEqual(t, 30.0)

    def test_timeout_zero_preserved(self) -> None:
        t, _ = maintenance.clamp_drain_params(0.0, 120.0)
        self.assertEqual(t, 0.0)

    def test_negative_timeout_becomes_default(self) -> None:
        t, _ = maintenance.clamp_drain_params(-5.0, 120.0)
        self.assertEqual(t, 20.0)

    def test_nan_timeout_becomes_default(self) -> None:
        t, _ = maintenance.clamp_drain_params(float("nan"), 120.0)
        self.assertEqual(t, 20.0)

    def test_inf_timeout_becomes_default(self) -> None:
        t, _ = maintenance.clamp_drain_params(float("inf"), 120.0)
        self.assertEqual(t, 20.0)

    def test_pause_capped_at_900(self) -> None:
        _, p = maintenance.clamp_drain_params(20.0, 1000.0)
        self.assertEqual(p, 900.0)

    def test_pause_floor_at_1(self) -> None:
        _, p = maintenance.clamp_drain_params(20.0, 0.5)
        self.assertEqual(p, 1.0)

    def test_invalid_string_timeout_becomes_default(self) -> None:
        t, _ = maintenance.clamp_drain_params("bad", 120.0)
        self.assertEqual(t, 20.0)


if __name__ == "__main__":
    unittest.main()