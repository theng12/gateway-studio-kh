#!/usr/bin/env python3
"""The operator key (`kho_`): one estate credential, scoped, revocable (0.2.297, step 1 of 6).

Step 1 is **additive** — the design's own sequencing says the lock before the door. So the tests
here pin three things and nothing more:

1. the store is one 0600 file in the hold-token shape, holding a SHA-256 and never the token;
2. a key is refused when it lacks the scope asked for, by name, and accepted when it carries it;
3. a request **without** a `kho_` token goes through `require_admin` exactly as it did before, so
   this change cannot have loosened a route.

What these tests do **not** prove: that an unkeyed call is refused on a loopback-bound install (that
is parity case 1, step 2) or that the old gateway key is refused (step 3).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-operator-key-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_OPERATOR_KEYS": str(base / "operator-keys.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "offer-fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, admin_session, api, operator_key  # noqa: E402

OWNER_SECRET = "test-owner-console-secret-0123456789"


class TheStore(unittest.TestCase):
    def setUp(self) -> None:
        operator_key.path().unlink(missing_ok=True)

    def test_a_key_is_returned_once_and_stored_only_as_a_hash(self) -> None:
        token, record = operator_key.mint("night shift", ["read"])
        self.assertTrue(token.startswith(operator_key.PREFIX))
        self.assertNotIn("sha256", record, "the public record must not carry the hash")
        stored = json.loads(operator_key.path().read_text())
        self.assertEqual(len(stored["keys"]), 1)
        self.assertEqual(stored["keys"][0]["sha256"], operator_key._digest(token))
        self.assertNotIn(token, operator_key.path().read_text(), "the token itself must never be stored")

    def test_the_file_is_0600(self) -> None:
        operator_key.mint("p", ["read"])
        mode = stat.S_IMODE(operator_key.path().stat().st_mode)
        self.assertEqual(mode, 0o600, f"the store must be 0600 like the hold token, not {oct(mode)}")

    def test_an_unknown_or_empty_scope_is_refused_at_issue(self) -> None:
        with self.assertRaises(ValueError):
            operator_key.mint("x", ["root"])
        with self.assertRaises(ValueError):
            operator_key.mint("x", [])

    def test_a_revoked_key_stops_matching_and_keeps_its_record(self) -> None:
        token, record = operator_key.mint("gone", ["read"])
        self.assertIsNotNone(operator_key.matches("Bearer " + token))
        self.assertTrue(operator_key.revoke(record["id"]))
        self.assertIsNone(operator_key.matches("Bearer " + token))
        listed = operator_key.list_keys()
        self.assertEqual(len(listed), 1, "a revoked key keeps its record")
        self.assertIsNotNone(listed[0]["revoked_at"])

    def test_the_gateway_key_and_junk_never_read_the_file(self) -> None:
        self.assertIsNone(operator_key.matches("Bearer offer-fixture-key"))
        self.assertIsNone(operator_key.matches("Bearer kho_not-a-real-key"))
        self.assertIsNone(operator_key.matches(None))

    def test_owner_scope_implies_the_lower_scopes_but_never_empty_trash(self) -> None:
        _token, record = operator_key.mint("o", ["owner"])
        self.assertTrue(operator_key.allows(record, "read"))
        self.assertTrue(operator_key.allows(record, "ops"))
        self.assertFalse(operator_key.allows(record, "empty-trash"),
                         "the wide destructive door is never implied by another scope")


class TheGuard(unittest.TestCase):
    """Parity cases 3 and 4 on the one pilot route (`GET /api/fleet`)."""

    def setUp(self) -> None:
        operator_key.path().unlink(missing_ok=True)
        admin_session._owner_secret = OWNER_SECRET
        self.addCleanup(setattr, admin_session, "_owner_secret", "")
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))

    def tearDown(self) -> None:
        self.client.close()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {admin_session.OWNER_HEADER: OWNER_SECRET}
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def test_a_key_with_the_scope_is_accepted(self) -> None:
        token, _record = operator_key.mint("reader", ["read"])
        response = self.client.get("/admin/api/fleet", headers=self._headers(token))
        self.assertEqual(response.status_code, 200)

    def test_a_key_without_the_scope_is_refused_and_names_it(self) -> None:
        token, _record = operator_key.mint("creator", ["create"])
        response = self.client.get("/admin/api/fleet", headers=self._headers(token))
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body.get("code"), "OPERATOR_SCOPE_REQUIRED")
        self.assertIn("read", json.dumps(body), f"the refusal must name the scope it wants: {body}")

    def test_a_revoked_key_is_refused_off_loopback(self) -> None:
        """A revoked key is no longer *the key*; away from loopback that means it is refused.

        On a loopback-bound install the revoked token falls through to `require_admin`, which still
        trusts this Mac — that is the bypass step 4 removes, not something this step can close.
        """
        token, record = operator_key.mint("reader", ["read"])
        operator_key.revoke(record["id"])
        away = TestClient(api.app, client=("10.9.8.7", 44444))
        self.addCleanup(away.close)
        response = away.get("/admin/api/fleet", headers={"Authorization": "Bearer " + token})
        self.assertNotEqual(response.status_code, 200,
                            "a revoked key authenticated off loopback")

    def test_the_old_path_is_unchanged_when_no_key_is_present(self) -> None:
        """The whole point of step 1: nothing that worked before stops working."""
        response = self.client.get("/admin/api/fleet", headers=self._headers())
        self.assertEqual(response.status_code, 200,
                         "a loopback caller with no kho_ key must still pass require_admin")

    def test_issue_and_revoke_are_the_owner_window_only(self) -> None:
        self.assertEqual(self.client.get("/admin/api/settings/operator-keys",
                                         headers=self._headers()).status_code, 200)
        without = self.client.get("/admin/api/settings/operator-keys",
                                  headers={"Authorization": "Bearer offer-fixture-key"})
        self.assertEqual(without.status_code, 403,
                         "listing credentials is owner information, not an admin-key action")
        issued = self.client.post("/admin/api/settings/operator-keys",
                                  headers=self._headers(),
                                  json={"name": "night shift", "scopes": ["read"]})
        self.assertEqual(issued.status_code, 200)
        self.assertTrue(issued.json()["key"].startswith(operator_key.PREFIX))
        key_id = issued.json()["record"]["id"]
        revoked = self.client.delete(f"/admin/api/settings/operator-keys/{key_id}",
                                     headers=self._headers())
        self.assertEqual(revoked.status_code, 200)
        self.assertIsNotNone(revoked.json()["keys"][0]["revoked_at"])


if __name__ == "__main__":
    unittest.main()
