#!/usr/bin/env python3
"""Guests: a named account that may use Create — and nothing else (owner, 2026-10-28).

The owner wants to share the Gateway with a friend over Tailscale. Today one password and the
gateway key both give the whole console; a friend must not get that. This suite holds the three
promises that make it safe, and the ones that would make it useless if broken:

1. **Deny by default.** Every registered `/admin` route refuses a guest session with 403,
   except the Create page's own routes, which are listed here. The walk below is over
   `api.app.routes` itself — a route added tomorrow without a thought is covered by the very
   test that would fail, rather than by somebody remembering to add it.
2. **Their own work only.** A guest sees, cancels, downloads and pins their own jobs. Another
   guest's job is answered exactly like a job that does not exist.
3. **Money.** Free and local models by default; paid models only after the owner switches
   them on, refused again on submit because a picker is a convenience and the refusal is the
   rule.

Plus the account lifecycle (create, disable, reset, retire — never a delete) and the job's
"Requested by", which must never be guessed.

Every test runs against a temp profile: nothing here touches the owner's guests, sessions,
jobs or settings.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-guests-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "guest-suite-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, admin_session, api  # noqa: E402
from gateway.runtime import store  # noqa: E402

FRIEND = ("100.100.1.1", 5555)   # a phone over Tailscale: not loopback, no key
PASSWORD = "correct horse battery"


def fresh_guests() -> None:
    """Every test starts with no guests and no sessions; the store is one process-wide object."""
    with admin_session._lock:
        admin_session._write(admin_session._guests_path(), {"guests": []})
        admin_session._loaded().clear()
        admin_session._write(admin_session._sessions_path(), {})


#: A write carried by a session cookie must also name this origin (`same_origin`).
SAME_ORIGIN = {"Origin": "http://testserver"}


def guest_cookie(name: str, password: str = PASSWORD, *, paid: bool = False) -> tuple[dict, dict]:
    row = admin_session.add_guest(name, password, paid_allowed=paid)
    token = admin_session.create_session(role="guest", guest_id=row["id"], name=row["name"])
    return row, {admin_session.COOKIE: token}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        fresh_guests()
        self.addCleanup(fresh_guests)
        self.made: list[str] = []

    def tearDown(self) -> None:
        if self.made:
            with store._lock:
                store._conn.execute(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(self.made))})",
                                    self.made)
                store._conn.commit()

    def job(self, *, requested_by: dict | None, modality: str = "chat") -> dict:
        row, _ = store.submit(modality=modality, model_id="mock-model", params={"text": "x"},
                              idempotency_key=f"guest-{len(self.made)}-{self.id()}",
                              provider_id="ui-fixture", requested_by=requested_by)
        self.made.append(row["id"])
        return row


# ---------------------------------------------------------------------------
# 1. Deny by default, walked over the real route table.
# ---------------------------------------------------------------------------
#: The only paths a guest may reach, spelled as the middleware spells them. Adding to this
#: list is a decision; the walk below is what makes forgetting one impossible.
GUEST_ALLOWED = {
    "/admin", "/admin/",
    "/admin/login", "/admin/logout",
    "/admin/api/create/models", "/admin/api/create", "/admin/api/create/transcription",
    "/admin/api/create/status/{job_id}", "/admin/api/create/status/{job_id}/cancel",
    "/admin/api/voices", "/admin/api/jobs", "/admin/api/artifacts/keep",
    "/admin/api/artifacts/{job_id}/outputs/{asset_id}",
}


def _walk(routes):
    """Every route, including the ones a mounted router keeps inside one wrapper entry.

    FastAPI 0.141 does not flatten `include_router` into `app.routes`: the whole router is one
    `_IncludedRouter` object whose routes live on `original_router`. Walking only the top level
    would have found **no** admin route at all — a test that passes because it checked nothing.
    """
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _walk(route.original_router.routes)
            continue
        path = getattr(route, "path", "")
        methods = sorted((getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"})
        if path.startswith("/admin") and methods:
            yield path, methods


def admin_routes():
    yield from _walk(api.app.routes)


class DenyByDefault(_Base):
    def test_the_allow_list_names_real_routes(self) -> None:
        known = {path for path, _methods in admin_routes()}
        unknown = GUEST_ALLOWED - known
        self.assertEqual(unknown, set(), "the allow-list names a path no route serves")

    def test_every_admin_route_refuses_a_guest(self) -> None:
        _row, cookies = guest_cookie("Maria")
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        allowed_seen: set[str] = set()
        for path, methods in admin_routes():
            if path in GUEST_ALLOWED:
                allowed_seen.add(path)
                # Allowed is not "open": it still needs the session, and this one has it.
                response = client.get(path.replace("{job_id}", "x").replace("{asset_id}", "0")
                                      if "GET" in methods else path.replace("{job_id}", "x"),
                                      cookies=cookies)
                self.assertNotIn(response.status_code, (401, 403), f"{path} refused its own guest")
                continue
            for method in methods:
                response = client.request(
                    method,
                    path.replace("{job_id}", "no-such-job").replace("{asset_id}", "0")
                        .replace("{controller_id}", "c").replace("{provider_id}", "p")
                        .replace("{voice_id}", "v").replace("{release_id}", "r")
                        .replace("{pack_id}", "p").replace("{machine}", "m")
                        .replace("{key}", "k").replace("{guest_id}", "g"),
                    cookies=cookies,
                )
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                self.assertEqual(response.status_code, 403,
                                 f"{method} {path} answered {response.status_code} to a guest")
                self.assertEqual(body.get("code"), "GUEST_NOT_ALLOWED", f"{method} {path}")
        self.assertEqual(allowed_seen, GUEST_ALLOWED,
                         "a route in the allow-list is missing from the app, so this walk proved less")

    def test_the_gateway_key_still_opens_the_console(self) -> None:
        # The guard must be about *guests*, not about the cookie: the key is the owner's.
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        response = client.get("/admin/api/guests",
                              headers={"Authorization": "Bearer guest-suite-key"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_guest_page_says_who_is_looking(self) -> None:
        row, cookies = guest_cookie("Maria")
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        page = client.get("/admin", cookies=cookies).text
        self.assertIn("window.__GATEWAY_GUEST__ = ", page)
        self.assertIn(row["name"], page.split("window.__GATEWAY_GUEST__ = ")[1][:200])

    def test_no_guest_marker_for_the_owner(self) -> None:
        client = TestClient(api.app, client=("127.0.0.1", 1234))
        self.addCleanup(client.close)
        self.assertNotIn("window.__GATEWAY_GUEST__ = ", client.get("/admin").text)


# ---------------------------------------------------------------------------
# 2. Accounts: create, disable, enable, reset, retire.
# ---------------------------------------------------------------------------
class Accounts(_Base):
    def test_only_a_scrypt_hash_is_stored(self) -> None:
        admin_session.add_guest("Maria", PASSWORD)
        raw = (BASE / "admin-guests.json").read_text()
        self.assertNotIn(PASSWORD, raw)
        record = json.loads(raw)["guests"][0]
        self.assertEqual(record["algorithm"], "scrypt")
        self.assertEqual(len(record["hash"]), 64)

    def test_a_short_password_and_a_blank_name_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            admin_session.add_guest("Maria", "short")
        with self.assertRaises(ValueError):
            admin_session.add_guest("   ", PASSWORD)

    def test_two_guests_cannot_share_a_name(self) -> None:
        admin_session.add_guest("Maria", PASSWORD)
        with self.assertRaises(ValueError):
            admin_session.add_guest("maria", PASSWORD)

    def test_disable_ends_the_session_and_enable_lets_them_back_in(self) -> None:
        row, cookies = guest_cookie("Maria")
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/admin/api/create/models", cookies=cookies).status_code, 200)
        admin_session.update_guest(row["id"], disabled=True)
        self.assertEqual(client.get("/admin/api/create/models", cookies=cookies).status_code, 401)
        self.assertIsNone(admin_session.check_guest("Maria", PASSWORD),
                          "a disabled guest cannot sign in")
        admin_session.update_guest(row["id"], disabled=False)
        self.assertIsNotNone(admin_session.check_guest("Maria", PASSWORD))
        self.assertEqual(client.get("/admin/api/create/models", cookies=cookies).status_code, 401,
                         "the old session stays dead; they sign in again")

    def test_a_password_reset_signs_the_guest_out_and_the_old_one_stops_working(self) -> None:
        row, cookies = guest_cookie("Maria")
        admin_session.set_guest_password(row["id"], "another good password")
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/admin/api/create/models", cookies=cookies).status_code, 401)
        self.assertIsNone(admin_session.check_guest("Maria", PASSWORD))
        self.assertIsNotNone(admin_session.check_guest("Maria", "another good password"))

    def test_remove_is_a_soft_retire(self) -> None:
        row, cookies = guest_cookie("Maria")
        retired = admin_session.retire_guest(row["id"])
        self.assertTrue(retired["removed"])
        self.assertEqual([g["id"] for g in admin_session.guests()], [row["id"]],
                         "the record stays, so its history can still be read")
        self.assertIsNone(admin_session.check_guest("Maria", PASSWORD))
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/admin/api/create/models", cookies=cookies).status_code, 401)
        # …and a retired name may be given to somebody else, because the old record is history.
        self.assertIsNotNone(admin_session.add_guest("Maria", "a different password"))

    def test_the_owner_password_change_does_not_revoke_guests(self) -> None:
        _row, cookies = guest_cookie("Maria")
        admin_session.set_password("owner password here")
        self.assertIsNotNone(admin_session.session(cookies[admin_session.COOKIE]))

    def test_an_old_session_file_still_means_the_owner(self) -> None:
        # The file used to be `{digest: last_used}`. Reading it as anything but an owner
        # session would sign the owner out on upgrade for no reason.
        token = "legacy-token"
        admin_session._sessions = None
        admin_session._write(admin_session._sessions_path(),
                             {admin_session._digest(token): 9e9})  # far future: never idle
        self.assertTrue(admin_session.valid_session(token))
        self.assertEqual(admin_session.session(token)["role"], "owner")
        admin_session._sessions = None


# ---------------------------------------------------------------------------
# 3. Someone else's job is not theirs.
# ---------------------------------------------------------------------------
class OwnWorkOnly(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.maria, self.maria_cookies = guest_cookie("Maria")
        self.anna, self.anna_cookies = guest_cookie("Anna")
        self.maria_job = self.job(requested_by={"role": "guest", "guest_id": self.maria["id"],
                                                "label": "Maria"})
        self.owner_job = self.job(requested_by={"role": "owner", "label": "Owner"})

    def client(self) -> TestClient:
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        return client

    def test_status_refuses_another_guests_job_and_answers_like_a_missing_one(self) -> None:
        client = self.client()
        own = client.get(f"/admin/api/create/status/{self.maria_job['id']}", cookies=self.maria_cookies)
        self.assertEqual(own.status_code, 200)
        other = client.get(f"/admin/api/create/status/{self.maria_job['id']}", cookies=self.anna_cookies)
        missing = client.get("/admin/api/create/status/no-such-job", cookies=self.anna_cookies)
        self.assertEqual(other.status_code, 404)
        # Byte-identical to the answer for an id that never existed: a guest cannot learn
        # which job ids are real by comparing two refusals.
        self.assertEqual(other.json()["detail"], f"no job '{self.maria_job['id']}'")
        self.assertEqual(missing.json()["detail"], "no job 'no-such-job'")
        self.assertEqual(set(other.json()), set(missing.json()))
        owners = client.get(f"/admin/api/create/status/{self.owner_job['id']}", cookies=self.maria_cookies)
        self.assertEqual(owners.status_code, 404, "the owner's work is not a guest's to read")

    def test_cancel_refuses_another_guests_job(self) -> None:
        client = self.client()
        response = client.post(f"/admin/api/create/status/{self.maria_job['id']}/cancel",
                               cookies=self.anna_cookies, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 404)

    def test_downloads_are_scoped(self) -> None:
        path = BASE / "artifacts" / f"{self.maria_job['id']}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        store.succeed(self.maria_job["id"], asset_path=str(path), media_type="text/plain",
                      artifacts=[{"asset_id": "0", "path": str(path), "media_type": "text/plain",
                                  "bytes": 5}], result=None)
        client = self.client()
        own = client.get(f"/admin/api/artifacts/{self.maria_job['id']}/outputs/0",
                         cookies=self.maria_cookies)
        self.assertEqual(own.status_code, 200)
        other = client.get(f"/admin/api/artifacts/{self.maria_job['id']}/outputs/0",
                           cookies=self.anna_cookies)
        self.assertEqual(other.status_code, 404)
        # The download form of the same route is refused identically.
        self.assertEqual(client.get(f"/admin/api/artifacts/{self.maria_job['id']}/outputs/0?download=1",
                                    cookies=self.anna_cookies).status_code, 404)

    def test_the_job_list_is_their_own(self) -> None:
        client = self.client()
        mine = client.get("/admin/api/jobs", params={"limit": 200}, cookies=self.maria_cookies).json()
        self.assertEqual([row["id"] for row in mine["jobs"]], [self.maria_job["id"]])
        theirs = client.get("/admin/api/jobs", params={"limit": 200}, cookies=self.anna_cookies).json()
        self.assertEqual(theirs["jobs"], [])
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        everything = owner.get("/admin/api/jobs", params={"limit": 200}).json()
        self.assertIn(self.maria_job["id"], [row["id"] for row in everything["jobs"]])
        self.assertIn(self.owner_job["id"], [row["id"] for row in everything["jobs"]])


# ---------------------------------------------------------------------------
# 4. Money: free and local by default, paid only by the owner's switch.
# ---------------------------------------------------------------------------
FREE_ROW = {"id": "ui-fixture::free-model", "display_name": "Free One", "chargeable": False}
PAID_ROW = {"id": "paid::costly", "display_name": "Costly", "chargeable": True}


class PaidModels(_Base):
    def setUp(self) -> None:
        super().setUp()
        from types import SimpleNamespace

        self.definition = SimpleNamespace(id="ui-fixture", display_name="Fixture", data_use="",
                                          kind="openai_compatible", free_tier=None)
        self.model = {"model_id": "free-model"}
        self.paid_definition = SimpleNamespace(id="paid", display_name="Paid Lane", data_use="",
                                               kind="openai_compatible", free_tier=None)
        self.paid_model = {"model_id": "costly"}
        patches = [
            patch.object(admin.registry.models, "enabled_models",
                         lambda modality: [(self.definition, self.model),
                                           (self.paid_definition, self.paid_model)]),
            patch.object(admin.catalog, "rows", lambda modality: [dict(FREE_ROW), dict(PAID_ROW)]),
            patch.object(admin.fleet, "publish_id", lambda provider_id: provider_id),
            patch.object(admin.fleet, "is_location_provider", lambda provider_id: False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self) -> TestClient:
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        return client

    def test_a_guest_is_offered_free_models_only_by_default(self) -> None:
        _row, cookies = guest_cookie("Maria")
        body = self.client().get("/admin/api/create/models", cookies=cookies).json()
        ids = [m["id"] for m in body["by_modality"]["chat"]]
        self.assertIn("ui-fixture::free-model", ids)
        self.assertNotIn("paid::costly", ids)
        self.assertFalse(body["paid_models"])

    def test_the_owner_switch_opens_paid_models(self) -> None:
        row, cookies = guest_cookie("Maria")
        admin_session.update_guest(row["id"], paid_allowed=True)
        body = self.client().get("/admin/api/create/models", cookies=cookies).json()
        self.assertIn("paid::costly", [m["id"] for m in body["by_modality"]["chat"]])
        self.assertTrue(body["paid_models"])

    def test_the_owner_still_sees_paid_models(self) -> None:
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        body = owner.get("/admin/api/create/models").json()
        self.assertIn("paid::costly", [m["id"] for m in body["by_modality"]["chat"]])

    def test_a_paid_model_is_refused_on_submit_even_if_the_page_is_bypassed(self) -> None:
        _row, cookies = guest_cookie("Maria")
        response = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN, json={
            "modality": "chat", "model_id": "paid::costly", "params": {"prompt": "hi"},
            "idempotency_key": "paid-bypass-1",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "GUEST_PAID_MODEL")
        self.assertEqual(store.find_by_idempotency_key("paid-bypass-1"), None,
                         "a refused submission must not queue anything")


# ---------------------------------------------------------------------------
# 5. Requested by, never guessed.
# ---------------------------------------------------------------------------
class RequestedBy(_Base):
    def test_a_guest_job_carries_the_guests_name(self) -> None:
        from types import SimpleNamespace

        row, cookies = guest_cookie("Maria")
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"),
                                                 {"model_id": "mock-chat-1"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        answer = client.post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN, json={
            "modality": "chat", "model_id": "ui-fixture::mock-chat-1",
            "params": {"prompt": "hi"}, "idempotency_key": "guest-asked",
        }).json()
        stored = store.find_by_idempotency_key("guest-asked")
        self.assertIsNotNone(stored, f"the guest's job was refused: {answer}")
        self.made.append(stored["id"])
        from gateway.store import requested_by_label, requested_by_of
        self.assertEqual(requested_by_of(stored)["role"], "guest")
        self.assertEqual(requested_by_of(stored)["label"], "Maria")
        self.assertEqual(requested_by_of(stored)["guest_id"], row["id"])
        self.assertEqual(requested_by_label(stored), "Maria")

    def test_an_owner_job_is_labelled_and_a_key_job_is_story_studio(self) -> None:
        from gateway.store import requested_by_label, requested_by_of

        owner = self.job(requested_by={"role": "owner", "label": "Owner"})
        self.assertEqual(requested_by_label(owner), "Owner")
        kited = self.job(requested_by={"role": "key", "label": "Story Studio"})
        self.assertEqual(requested_by_of(kited)["role"], "key")
        self.assertEqual(requested_by_label(kited), "Story Studio")

    def test_a_job_from_before_the_field_reads_not_recorded(self) -> None:
        from gateway.store import requested_by_label, requested_by_of

        old = self.job(requested_by=None)
        self.assertIsNone(requested_by_of(old))
        self.assertEqual(requested_by_label(old), "not recorded")

    def test_an_unknown_role_is_dropped_rather_than_stored(self) -> None:
        from gateway.store import requested_by_of

        row = self.job(requested_by={"role": "wizard", "label": "Someone"})
        self.assertIsNone(requested_by_of(row))
        # A guest-shaped request without a guest id is also not a guest claim.
        row = self.job(requested_by={"role": "guest", "label": "Nobody"})
        self.assertIsNone(requested_by_of(row))

    def test_the_job_list_publishes_the_label(self) -> None:
        job = self.job(requested_by={"role": "owner", "label": "Owner"})
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        rows = owner.get("/admin/api/jobs", params={"limit": 200}).json()["jobs"]
        row = next(r for r in rows if r["id"] == job["id"])
        self.assertEqual(row["requested_by_label"], "Owner")
        self.assertEqual(row["requested_by"]["role"], "owner")


if __name__ == "__main__":
    unittest.main()
