#!/usr/bin/env python3
"""A guest's API key for an agent: the same guest, one bearer token (owner, 2026-10-30).

The owner wants agents (Claude Code, Claude Desktop) to make assets through his Gateway,
as people use hosted generators. An agent cannot hold a browser cookie, so a guest may be
given **one** API key. The promises this suite holds, each with a test that fails if it
breaks:

1. **It is the same guest, not a second account.** Free and local models only unless the
   owner allowed paid ones; its own jobs and files only; "Requested by <name> (guest)" on
   every job it starts.
2. **Deny by default.** Every `/admin` route that refuses a guest session refuses this key
   too, and **every `/v1` route refuses it** — `/v1` is the owner's API (fleet, account,
   content, final renders) and a guest never reaches it. Walked over the real route table.
3. **The key is shown once and stored hashed.** No response but the issue call carries it;
   the file holds a SHA-256; the page shows a fingerprint.
4. **Revocation is immediate**, and disabling or removing the guest kills it too.

Temp profile throughout: nothing here touches the owner's guests, sessions, jobs or settings.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-guest-keys-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "guest-key-suite-0123456789abcdef",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, admin_session, api  # noqa: E402
from gateway.api import idempotency_scope, scoped_idempotency_key  # noqa: E402
from gateway.runtime import store  # noqa: E402
from gateway.store import requested_by_label, requested_by_of  # noqa: E402

FRIEND = ("100.100.1.1", 5555)      # a Mac over Tailscale: not loopback
LOCAL = ("127.0.0.1", 1234)
PASSWORD = "correct horse battery"
OWNER_KEY = "guest-key-suite-0123456789abcdef"


def fresh_guests() -> None:
    with admin_session._lock:
        admin_session._write(admin_session._guests_path(), {"guests": []})
        admin_session._loaded().clear()
        admin_session._write(admin_session._sessions_path(), {})


def guest_with_key(name: str, *, paid: bool = False) -> tuple[dict, str]:
    row = admin_session.add_guest(name, PASSWORD, paid_allowed=paid)
    return row, admin_session.issue_guest_key(row["id"])


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        fresh_guests()
        self.addCleanup(fresh_guests)
        self.made: list[str] = []

    def tearDown(self) -> None:
        if self.made:
            with store._lock:
                store._conn.execute(
                    f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(self.made))})", self.made)
                store._conn.commit()

    def client(self, address=FRIEND) -> TestClient:
        client = TestClient(api.app, client=address)
        self.addCleanup(client.close)
        return client

    def job(self, *, requested_by: dict | None, modality: str = "chat") -> dict:
        row, _ = store.submit(modality=modality, model_id="mock-model", params={"text": "x"},
                              idempotency_key=f"key-{len(self.made)}-{self.id()}",
                              provider_id="ui-fixture", requested_by=requested_by)
        self.made.append(row["id"])
        return row


# ---------------------------------------------------------------------------
# 1. The key itself: format, storage, one showing, replacement.
# ---------------------------------------------------------------------------
class TheKey(_Base):
    def test_it_is_a_prefixed_random_token_stored_only_as_a_hash(self) -> None:
        row, key = guest_with_key("Maria")
        self.assertTrue(key.startswith(admin_session.GUEST_KEY_PREFIX))
        self.assertGreaterEqual(len(key), 40)
        raw = (BASE / "admin-guests.json").read_text()
        self.assertNotIn(key, raw, "the key itself must never be on disk")
        record = json.loads(raw)["guests"][0]["api_key"]
        self.assertEqual(len(record["hash"]), 64, "a SHA-256 digest")
        self.assertIsNone(record["revoked_at"])

    def test_the_list_shows_a_fingerprint_and_never_the_key(self) -> None:
        row, key = guest_with_key("Maria")
        client = self.client()
        answer = client.get("/admin/api/guests", headers=bearer(OWNER_KEY))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertNotIn(key, answer.text)
        listed = answer.json()["guests"][0]
        self.assertTrue(listed["api_key"]["fingerprint"])
        self.assertFalse(listed["api_key"]["revoked"])
        self.assertNotIn(listed["api_key"]["fingerprint"] * 2, key)

    def test_issuing_again_replaces_the_old_key(self) -> None:
        row, first = guest_with_key("Maria")
        second = admin_session.issue_guest_key(row["id"])
        self.assertNotEqual(first, second)
        client = self.client()
        self.assertEqual(client.get("/admin/api/create/models", headers=bearer(first)).status_code,
                         401, "the replaced key stops working")
        self.assertEqual(client.get("/admin/api/create/models", headers=bearer(second)).status_code,
                         200)

    def test_the_issue_route_returns_the_key_once_and_is_owner_only(self) -> None:
        row = admin_session.add_guest("Maria", PASSWORD)
        client = self.client()
        owner = client.post(f"/admin/api/guests/{row['id']}/key", headers=bearer(OWNER_KEY))
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual(owner.headers.get("cache-control"), "no-store")
        key = owner.json()["key"]
        self.assertTrue(key.startswith(admin_session.GUEST_KEY_PREFIX))
        # A guest cannot mint, see or revoke keys.
        guest, guest_key = guest_with_key("Sam")
        refused = client.post(f"/admin/api/guests/{row['id']}/key", headers=bearer(guest_key))
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["code"], "GUEST_NOT_ALLOWED")
        self.assertEqual(client.delete(f"/admin/api/guests/{row['id']}/key",
                                       headers=bearer(guest_key)).status_code, 403)


# ---------------------------------------------------------------------------
# 2. Deny by default, walked over the real route table.
# ---------------------------------------------------------------------------
GUEST_ALLOWED = {
    "/admin", "/admin/",
    "/admin/login", "/admin/logout",
    "/admin/api/create/models", "/admin/api/create", "/admin/api/create/transcription",
    "/admin/api/create/video",
    "/admin/api/create/status/{job_id}", "/admin/api/create/status/{job_id}/cancel",
    "/admin/api/voices", "/admin/api/jobs", "/admin/api/artifacts/keep",
    "/admin/api/artifacts/{job_id}/outputs/{asset_id}",
}


def _walk(routes, prefix: str):
    """Every route under `prefix`, including a mounted router's own table (FastAPI 0.141)."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _walk(route.original_router.routes, prefix)
            continue
        path = getattr(route, "path", "")
        methods = sorted((getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"})
        if path.startswith(prefix) and methods:
            yield path, methods


def routes_under(prefix: str):
    yield from _walk(api.app.routes, prefix)


class DenyByDefault(_Base):
    def test_the_allow_list_names_real_routes(self) -> None:
        known = {path for path, _methods in routes_under("/admin")}
        self.assertEqual(GUEST_ALLOWED - known, set())

    def _filled(self, path: str) -> str:
        return (path.replace("{job_id}", "no-such-job").replace("{asset_id}", "0")
                .replace("{controller_id}", "c").replace("{provider_id}", "p")
                .replace("{voice_id}", "v").replace("{release_id}", "r")
                .replace("{pack_id}", "p").replace("{machine}", "m")
                .replace("{key}", "k").replace("{guest_id}", "g"))

    def test_every_admin_route_refuses_a_guest_key(self) -> None:
        _row, key = guest_with_key("Maria")
        _owner, guest_key = guest_with_key("Anna")
        client = self.client()
        allowed_seen: set[str] = set()
        for path, methods in routes_under("/admin"):
            if path in GUEST_ALLOWED:
                allowed_seen.add(path)
                response = client.get(self._filled(path), headers=bearer(key))
                self.assertNotIn(response.status_code, (401, 403),
                                 f"{path} refused its own guest key")
                continue
            for method in methods:
                response = client.request(method, self._filled(path), headers=bearer(key))
                body = (response.json()
                        if response.headers.get("content-type", "").startswith("application/json")
                        else {})
                # An upload door refuses the key before the body (401); everything else is
                # the guest refusal itself (403). Both are "no", and neither reads the body.
                self.assertIn(response.status_code, (401, 403),
                              f"{method} {path} answered {response.status_code}: {response.text}")
                self.assertIn(body.get("code"), {"GUEST_NOT_ALLOWED", "UNAUTHENTICATED_UPLOAD"},
                              f"{method} {path}: {body.get('code')}")
        self.assertEqual(allowed_seen, GUEST_ALLOWED,
                         "a route in the allow-list is missing, so this walk proved less")

    def test_a_guest_key_is_not_the_owner_on_loopback_either(self) -> None:
        _row, key = guest_with_key("Maria")
        local = self.client(LOCAL)
        for path in ("/admin/api/guests", "/admin/api/settings", "/admin/api/backup/status"):
            response = local.get(path, headers=bearer(key))
            self.assertEqual(response.status_code, 403, f"{path}: {response.text}")
            self.assertEqual(response.json().get("code"), "GUEST_NOT_ALLOWED", path)
        # A write the owner's own Mac may make: a guest key may not.
        self.assertEqual(local.put("/admin/api/settings/browser-password", headers=bearer(key),
                                   json={"password": "a new owner password"}).status_code, 403)

    def test_every_v1_route_refuses_a_guest_key(self) -> None:
        """`/v1` is the owner's API. It is not a place a guest key opens, for any route."""
        _row, key = guest_with_key("Maria")
        client = self.client()
        checked = 0
        for path, methods in routes_under("/v1"):
            for method in methods:
                try:
                    response = client.request(method, self._filled(path), headers=bearer(key),
                                              json={} if method in ("POST", "PUT", "PATCH") else None)
                except Exception as exc:  # noqa: BLE001 - a transport error is still no access
                    self.fail(f"{method} {path} raised {exc!r}")
                checked += 1
                self.assertNotIn(response.status_code, (200, 201, 202, 204, 206),
                                 f"{method} {path} answered {response.status_code}: {response.text}")
                if response.headers.get("content-type", "").startswith("application/json"):
                    code = (response.json() or {}).get("code")
                    if code is not None:
                        self.assertIn(code, {"GUEST_NOT_ALLOWED", "UNAUTHORIZED", "FORBIDDEN",
                                             "UNAUTHENTICATED_UPLOAD"},
                                      f"{method} {path}: {code}")
        self.assertGreater(checked, 50, "the walk must cover the real /v1 surface")

    def test_a_dead_key_says_so_and_never_whether_it_ever_existed(self) -> None:
        _row, key = guest_with_key("Maria")
        admin_session.revoke_guest_key(admin_session.guests()[0]["id"])
        client = self.client()
        dead = client.get("/admin/api/create/models", headers=bearer(key))
        never = client.get("/admin/api/create/models",
                           headers=bearer(admin_session.GUEST_KEY_PREFIX + "never-existed"))
        self.assertEqual(dead.status_code, 401)
        self.assertEqual(dead.json()["code"], "GUEST_KEY_INVALID")
        self.assertEqual(dead.json(), never.json(), "one sentence for every dead key")


# ---------------------------------------------------------------------------
# 3. The same guest: free models, own work, requested-by.
# ---------------------------------------------------------------------------
FREE_ROW = {"id": "ui-fixture::free-model", "display_name": "Free One", "chargeable": False}
PAID_ROW = {"id": "paid::costly", "display_name": "Costly", "chargeable": True}


class TheSameGuest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.definition = SimpleNamespace(id="ui-fixture", display_name="Fixture", data_use="",
                                          kind="openai_compatible", free_tier=None)
        self.model = {"model_id": "free-model"}
        self.paid_definition = SimpleNamespace(id="paid", display_name="Paid Lane", data_use="",
                                               kind="openai_compatible", free_tier=None)
        self.paid_model = {"model_id": "costly"}
        for patcher in (
            patch.object(admin.registry.models, "enabled_models",
                         lambda modality: [(self.definition, self.model),
                                           (self.paid_definition, self.paid_model)]),
            patch.object(admin.catalog, "rows", lambda modality: [dict(FREE_ROW), dict(PAID_ROW)]),
            patch.object(admin.fleet, "publish_id", lambda provider_id: provider_id),
            patch.object(admin.fleet, "is_location_provider", lambda provider_id: False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self, key: str, model_id: str, *, modality: str = "chat",
               idempotency_key: str = "") -> object:
        return self.client().post(
            "/admin/api/create", headers={**bearer(key)},
            json={"modality": modality, "model_id": model_id, "params": {"prompt": "hi"},
                  "idempotency_key": idempotency_key or f"key-{len(self.made)}-{self.id()}"})

    def test_free_models_only_until_the_owner_switches_paid_on(self) -> None:
        row, key = guest_with_key("Maria")
        body = self.client().get("/admin/api/create/models", headers=bearer(key)).json()
        offered = [m["id"] for m in body["by_modality"]["chat"]]
        self.assertIn("ui-fixture::free-model", offered)
        self.assertNotIn("paid::costly", offered)
        self.assertFalse(body["paid_models"])
        refusal = self.submit(key, "paid::costly")
        self.assertEqual(refusal.status_code, 403, refusal.text)
        self.assertEqual(refusal.json()["code"], "GUEST_PAID_MODEL")
        admin_session.update_guest(row["id"], paid_allowed=True)
        accepted = self.submit(key, "paid::costly")
        self.assertIn(accepted.status_code, (200, 202), accepted.text)

    def test_a_job_it_starts_says_requested_by_the_guest(self) -> None:
        row, key = guest_with_key("Maria")
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"),
                                                 {"model_id": "mock-chat-1"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        answer = self.submit(key, "ui-fixture::mock-chat-1", idempotency_key="agent-asked")
        self.assertIn(answer.status_code, (200, 202), answer.text)
        stored = store.find_by_idempotency_key(scoped_idempotency_key(
            idempotency_scope({"role": "guest", "guest_id": row["id"]}), "agent-asked"))
        self.assertIsNotNone(stored)
        self.made.append(stored["id"])
        self.assertEqual(requested_by_of(stored)["role"], "guest")
        self.assertEqual(requested_by_of(stored)["guest_id"], row["id"])
        self.assertEqual(requested_by_label(stored), "Maria (guest)")
        # The other guest's key stored the same plaintext key somewhere else entirely.
        self.assertIsNone(store.find_by_idempotency_key("agent-asked"),
                          "the key is scoped, so a guest cannot replay the owner's")

    def test_no_origin_header_is_needed_for_a_bearer_key(self) -> None:
        """CSRF is about cookies. An agent sends no Origin and must not be refused for it."""
        _row, key = guest_with_key("Maria")
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"),
                                                 {"model_id": "mock-chat-1"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        answer = self.submit(key, "ui-fixture::mock-chat-1")
        self.assertIn(answer.status_code, (200, 202), answer.text)
        # …and a browser cookie is still held to the same-origin rule.
        token = admin_session.create_session(role="guest",
                                             guest_id=admin_session.guests()[0]["id"], name="Maria")
        cookie = {admin_session.COOKIE: token}
        cross = self.client().post("/admin/api/create", cookies=cookie,
                                   json={"modality": "chat", "model_id": "ui-fixture::free-model"})
        self.assertEqual(cross.status_code, 403)


class OwnWorkOnly(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.maria, self.maria_key = guest_with_key("Maria")
        self.anna, self.anna_key = guest_with_key("Anna")
        self.maria_job = self.job(requested_by={"role": "guest", "guest_id": self.maria["id"],
                                                "label": "Maria (guest)"})
        self.owner_job = self.job(requested_by={"role": "owner", "label": "Owner"})

    def test_status_and_cancel_are_their_own(self) -> None:
        client = self.client()
        self.assertEqual(client.get(f"/admin/api/create/status/{self.maria_job['id']}",
                                    headers=bearer(self.maria_key)).status_code, 200)
        other = client.get(f"/admin/api/create/status/{self.maria_job['id']}",
                           headers=bearer(self.anna_key))
        missing = client.get("/admin/api/create/status/no-such-job",
                             headers=bearer(self.anna_key))
        self.assertEqual(other.status_code, 404)
        self.assertEqual(set(other.json()), set(missing.json()),
                         "another guest's job reads exactly like one that never existed")
        self.assertEqual(other.json()["detail"], f"no job '{self.maria_job['id']}'")
        self.assertEqual(client.get(f"/admin/api/create/status/{self.owner_job['id']}",
                                    headers=bearer(self.maria_key)).status_code, 404)
        self.assertEqual(client.post(f"/admin/api/create/status/{self.maria_job['id']}/cancel",
                                     headers=bearer(self.anna_key)).status_code, 404)

    def test_downloads_are_scoped(self) -> None:
        path = BASE / "artifacts" / f"{self.maria_job['id']}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        store.succeed(self.maria_job["id"], asset_path=str(path), media_type="text/plain",
                      artifacts=[{"asset_id": "0", "path": str(path),
                                  "media_type": "text/plain", "bytes": 5}], result=None)
        client = self.client()
        own = client.get(f"/admin/api/artifacts/{self.maria_job['id']}/outputs/0?download=1",
                         headers=bearer(self.maria_key))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.content, b"hello")
        self.assertEqual(client.get(f"/admin/api/artifacts/{self.maria_job['id']}/outputs/0",
                                    headers=bearer(self.anna_key)).status_code, 404)

    def test_the_job_list_is_their_own(self) -> None:
        client = self.client()
        mine = client.get("/admin/api/jobs", params={"limit": 200},
                          headers=bearer(self.maria_key)).json()["jobs"]
        self.assertEqual([row["id"] for row in mine], [self.maria_job["id"]])
        theirs = client.get("/admin/api/jobs", params={"limit": 200},
                            headers=bearer(self.anna_key)).json()["jobs"]
        self.assertEqual(theirs, [])


# ---------------------------------------------------------------------------
# 4. Revocation, disabling, removal — on the next request, not on a restart.
# ---------------------------------------------------------------------------
class Lifecycle(_Base):
    def test_revoking_ends_the_key_on_the_next_request(self) -> None:
        row, key = guest_with_key("Maria")
        client = self.client()
        self.assertEqual(client.get("/admin/api/create/models", headers=bearer(key)).status_code,
                         200)
        admin_session.revoke_guest_key(row["id"])
        after = client.get("/admin/api/create/models", headers=bearer(key))
        self.assertEqual(after.status_code, 401)
        self.assertEqual(after.json()["code"], "GUEST_KEY_INVALID")
        # The account itself still exists and may be given a new key.
        renewed = admin_session.issue_guest_key(row["id"])
        self.assertEqual(client.get("/admin/api/create/models",
                                    headers=bearer(renewed)).status_code, 200)

    def test_disabling_and_removing_the_guest_end_the_key(self) -> None:
        row, key = guest_with_key("Maria")
        client = self.client()
        admin_session.update_guest(row["id"], disabled=True)
        disabled = client.get("/admin/api/create/models", headers=bearer(key))
        self.assertIn(disabled.status_code, (401, 403))
        admin_session.update_guest(row["id"], disabled=False)
        self.assertEqual(client.get("/admin/api/create/models", headers=bearer(key)).status_code,
                         200, "re-enabling the guest restores the key it already had")
        admin_session.retire_guest(row["id"])
        self.assertIn(client.get("/admin/api/create/models",
                                 headers=bearer(key)).status_code, (401, 403))

    def test_a_password_reset_does_not_kill_the_agents_key(self) -> None:
        row, key = guest_with_key("Maria")
        admin_session.set_guest_password(row["id"], "a different password")
        self.assertEqual(self.client().get("/admin/api/create/models",
                                           headers=bearer(key)).status_code, 200)


# ---------------------------------------------------------------------------
# 5. Image-to-video: the source image is a required field, and it is staged.
# ---------------------------------------------------------------------------
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class VideoFromAnImage(_Base):
    def test_the_scene_image_is_required_and_staged(self) -> None:
        _row, key = guest_with_key("Maria")
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"),
                                                 {"model_id": "mock-video-1"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        client = self.client()
        # No file at all: FastAPI refuses the missing required field before any job exists.
        self.assertEqual(client.post("/admin/api/create/video", headers=bearer(key),
                                     data={"model_id": "ui-fixture::mock-video-1"}).status_code,
                         422)
        answer = client.post("/admin/api/create/video", headers=bearer(key),
                             data={"model_id": "ui-fixture::mock-video-1", "prompt": "drift",
                                   "idempotency_key": "video-asked"},
                             files={"file": ("scene.png", io.BytesIO(PNG), "image/png")})
        self.assertIn(answer.status_code, (200, 202), answer.text)
        job = store.get(answer.json()["id"])
        self.made.append(job["id"])
        self.assertEqual(job["modality"], "video")
        params = json.loads(job["params"]) if isinstance(job["params"], str) else job["params"]
        self.assertEqual(params["_reference_type"], "image/png")
        self.assertTrue(Path(params["_reference_path"]).is_file(),
                        "the job keeps its own copy of the scene image")
        self.assertEqual(requested_by_label(job), "Maria (guest)")

    def test_transcription_uploads_through_the_create_door(self) -> None:
        _row, key = guest_with_key("Maria")
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture",
                                                                 display_name="Fixture",
                                                                 data_use="",
                                                                 kind="openai_compatible",
                                                                 free_tier=None),
                                                 {"model_id": "mock-asr-1", "display_name": "x"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        answer = self.client().post(
            "/admin/api/create/transcription", headers=bearer(key),
            data={"model_id": "ui-fixture::mock-asr-1", "language": "km",
                  "idempotency_key": "asr-asked"},
            files={"file": ("clip.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")})
        self.assertIn(answer.status_code, (200, 202), answer.text)
        job = store.get(answer.json()["id"])
        self.made.append(job["id"])
        self.assertEqual(job["modality"], "transcription")
        self.assertEqual(requested_by_label(job), "Maria (guest)")

    def test_a_guest_key_does_not_open_another_upload_door(self) -> None:
        """The key is a credential for the Create upload doors and no others."""
        _row, key = guest_with_key("Maria")
        answer = self.client().post(
            "/v1/audio/transcriptions/quotes", headers=bearer(key),
            files={"file": ("clip.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")})
        self.assertIn(answer.status_code, (401, 403), answer.text)
        self.assertEqual(answer.json().get("code"), "UNAUTHENTICATED_UPLOAD")

    def test_an_internal_field_cannot_be_named_in_options(self) -> None:
        _row, key = guest_with_key("Maria")
        answer = self.client().post(
            "/admin/api/create/video", headers=bearer(key),
            data={"model_id": "ui-fixture::mock-video-1",
                  "options": json.dumps({"_reference_path": "/etc/passwd"})},
            files={"file": ("scene.png", io.BytesIO(PNG), "image/png")})
        self.assertEqual(answer.status_code, 422)
        self.assertEqual(answer.json()["code"], "INTERNAL_PARAM")


if __name__ == "__main__":
    unittest.main()
